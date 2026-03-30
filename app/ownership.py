from __future__ import annotations

from datetime import datetime, timezone
from difflib import SequenceMatcher
import json
from pathlib import Path
import re
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup
import requests
from psycopg.types.json import Json

from app.config import get_settings
from app.db import LOCK_NAMESPACE_OWNERSHIP, advisory_job_lock, ensure_schema, get_connection


OWNERSHIP_PARSER_VERSION = "newark-ownership-v1"
CONFIDENCE_PROBABLE = "probable"
CONFIDENCE_ALLOWED = {CONFIDENCE_PROBABLE}
STATUS_NOT_STARTED = "not_started"
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_NEEDS_REVIEW = "needs_review"

LEGAL_SUFFIXES = {
    "llc",
    "l.l.c",
    "inc",
    "corp",
    "corporation",
    "co",
    "company",
    "ltd",
    "lp",
    "l.p",
    "llp",
    "l.l.p",
    "pllc",
    "l.l.c.",
    "inc.",
    "corp.",
    "co.",
    "ltd.",
}

DEED_DOCUMENT_TYPE = "DEED"
OWNERSHIP_STALE_CLAIM_DAYS = 90

UNKNOWN_OWNER_VALUES = {
    "",
    "unknown owner",
    "unknown",
    "n/a",
    "na",
    "not listed",
    "none",
    "unavailable",
}


def _utc_now():
    return datetime.now(timezone.utc)


def _log_step(progress, message):
    if progress:
        progress(message)


def _slugify(text):
    return "-".join(
        part for part in re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split() if part
    )


def _normalize_space(text):
    return " ".join((text or "").replace("\xa0", " ").split())


def normalize_org_name(name):
    tokens = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split()
    while tokens and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _sequence_similarity(left, right):
    return SequenceMatcher(None, normalize_org_name(left), normalize_org_name(right)).ratio()


def _parse_date(value):
    cleaned = _normalize_space(value)
    if not cleaned:
        return None
    for fmt in ("%m/%d/%Y", "%m/%Y", "%m/%d/%y"):
        try:
            parsed = datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
        if fmt == "%m/%Y":
            return parsed.replace(day=1)
        return parsed
    return None


def _parse_money(value):
    cleaned = _normalize_space(value)
    if not cleaned:
        return None
    normalized = cleaned.replace("$", "").replace(",", "")
    normalized = re.sub(r"[^0-9.\-]", "", normalized)
    if not normalized:
        return None
    try:
        return float(normalized)
    except ValueError:
        return None


def _classifier_document_category(document_type):
    normalized_type = (document_type or "").strip().upper()
    if normalized_type == DEED_DOCUMENT_TYPE:
        return "deed"
    if "ASSIGNMENT OF MORTGAGE" in normalized_type or (
        "ASSIGNMENT" in normalized_type and "MORTGAGE" in normalized_type
    ):
        return "assignment_of_mortgage"
    if "MORTGAGE" in normalized_type:
        return "mortgage"
    if "LIS PENDENS" in normalized_type:
        return "lis_pendens"
    if "UCC" in normalized_type:
        return "ucc"
    if "LIEN" in normalized_type:
        return "lien"
    return "other"


def _is_lender_related(document_type):
    return _classifier_document_category(document_type) in {
        "mortgage",
        "assignment_of_mortgage",
        "lien",
        "lis_pendens",
        "ucc",
    }


def _cache_access_date(path):
    if path and path.exists():
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).date().isoformat()
    return _utc_now().date().isoformat()


def _json_ready(value):
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except TypeError:
            pass
    return value


def _owner_name_is_missing(owner_name):
    normalized = _normalize_space(owner_name).lower()
    return normalized in UNKNOWN_OWNER_VALUES


def _qa_reason_payload(status, message, code, details=None, severity="warning"):
    return {
        "status": status,
        "message": message,
        "code": code,
        "severity": severity,
        "details": details or {},
    }


def _evaluate_ownership_qc(cursor, parcel_id, owner_name, recorded_at, confidence_tier, now):
    qa = {
        "issues": [],
        "warnings": [],
        "active_claim_count": 0,
        "active_claim_ids": [],
    }

    cursor.execute(
        """
        SELECT
            id,
            owner_name,
            confidence_tier,
            valid_from,
            last_enriched_at,
            valid_to
        FROM parcel_ownership_claims
        WHERE parcel_id = %s
          AND valid_to IS NULL
        ORDER BY COALESCE(valid_from, DATE '1900-01-01') DESC, created_at DESC
        """,
        (parcel_id,),
    )
    active_claims = cursor.fetchall()
    qa["active_claim_count"] = len(active_claims)
    qa["active_claim_ids"] = [claim["id"] for claim in active_claims]

    if qa["active_claim_count"] > 1:
        qa["issues"].append(
            _qa_reason_payload(
                "failed",
                "Multiple active ownership claims exist for this parcel. Temporal merging required before promotion.",
                "duplicate_active_claims",
                {
                    "active_claim_count": qa["active_claim_count"],
                    "active_claim_ids": qa["active_claim_ids"],
                },
                severity="error",
            )
        )

    if _owner_name_is_missing(owner_name):
        qa["issues"].append(
            _qa_reason_payload(
                "failed",
                "Deed owner value is missing or unresolved.",
                "missing_grantee_owner",
                {"owner_name": owner_name},
                severity="error",
            )
        )

    if confidence_tier and confidence_tier not in CONFIDENCE_ALLOWED:
        qa["issues"].append(
            _qa_reason_payload(
                "failed",
                "Confidence tier is outside expected allowable source policy for Essex recorder-derived ownership.",
                "confidence_mismatch",
                {"confidence_tier": confidence_tier, "allowed_tiers": sorted(CONFIDENCE_ALLOWED)},
                severity="error",
            )
        )

    if recorded_at:
        recorded_age_days = (now.date() - recorded_at.date()).days if isinstance(recorded_at, datetime) else (now.date() - recorded_at).days
        if recorded_age_days > 3650:
            qa["warnings"].append(
                _qa_reason_payload(
                    "needs_review",
                    "Deed recording date is very old and should be manually confirmed.",
                    "stale_deed_record",
                    {
                        "recorded_at": recorded_at.isoformat(),
                        "recorded_age_days": recorded_age_days,
                    },
                    severity="warning",
                )
            )

    for claim in active_claims:
        if not claim["last_enriched_at"]:
            continue
        age_days = (now - claim["last_enriched_at"]).days
        if age_days > OWNERSHIP_STALE_CLAIM_DAYS:
            qa["warnings"].append(
                _qa_reason_payload(
                    "needs_review",
                    "Ownership record is past the freshness threshold and should be revalidated.",
                    "stale_ownership_enrichment",
                    {
                        "claim_id": claim["id"],
                        "ownership_last_enriched_at": claim["last_enriched_at"].isoformat(),
                        "age_days": age_days,
                        "stale_threshold_days": OWNERSHIP_STALE_CLAIM_DAYS,
                    },
                    severity="warning",
                )
            )

    if qa["issues"]:
        status = STATUS_FAILED
    elif qa["warnings"]:
        status = STATUS_NEEDS_REVIEW
    else:
        status = STATUS_COMPLETED

    return {
        "status": status,
        "qa": qa,
        "review_reasons": [item["message"] for item in (qa["issues"] + qa["warnings"])],
    }


def _build_form_payload(form):
    payload = {}
    for tag in form.find_all(["input", "select", "textarea"]):
        name = tag.get("name")
        if not name:
            continue
        if tag.name == "select":
            option = tag.find("option", selected=True) or tag.find("option")
            payload[name] = option.get("value", "") if option else ""
            continue
        field_type = tag.get("type", "text").lower()
        if field_type in {"submit", "button", "image"}:
            continue
        if field_type in {"checkbox", "radio"} and not tag.has_attr("checked"):
            continue
        payload[name] = tag.get("value", "")
    return payload


def _normalize_header(text):
    normalized = re.sub(r"[^a-z0-9]+", " ", (text or "").lower())
    return " ".join(normalized.split())


def _parse_essex_header_map(header_cells):
    if not header_cells:
        return {}

    header_map = {}
    for index, cell in enumerate(header_cells):
        label = _normalize_header(cell.get_text(" ", strip=True))
        if not label:
            continue

        if ("document" in label and "type" in label) or "doc type" in label:
            header_map["document_type"] = index
            continue
        if "instrument" in label or "document number" in label:
            header_map["document_number"] = index
            continue
        if "direct party" in label:
            header_map["direct_party"] = index
            continue
        if "indirect party" in label:
            header_map["indirect_party"] = index
            continue
        if "town" in label or "municipal" in label:
            header_map["municipality"] = index
            continue
        if label == "block":
            header_map["block"] = index
            continue
        if label == "lot":
            header_map["lot"] = index
            continue
        if "recorded" in label:
            header_map["recorded_at"] = index
            continue
        if label == "book":
            header_map["book"] = index
            continue
        if label == "page":
            header_map["page"] = index
            continue
        if "consideration" in label or "amount" in label:
            header_map["consideration"] = index

    return header_map


def _header_to_index_map():
    return {
        "document_type": 0,
        "direct_party": 1,
        "indirect_party": 2,
        "document_number": 3,
        "recorded_at": 4,
        "municipality": 5,
        "block": 6,
        "lot": 7,
        "book": 8,
        "page": 9,
        "consideration": None,
    }


def _extract_cell_text(cells, columns, key, fallback_index):
    index = columns.get(key)
    if index is None:
        index = fallback_index
    if index is None or index >= len(cells):
        return ""
    return _normalize_space(cells[index].get_text(" ", strip=True))


def parse_essex_search_results(html):
    soup = BeautifulSoup(html, "html.parser")
    no_results = soup.find(string=lambda text: text and "No results found" in text)
    if no_results:
        return []

    table = soup.find("table", id="ctl00_ContentPlaceHolder1_dgdDeedMort")
    if not table:
        return []

    columns = _header_to_index_map()
    for row in table.find_all("tr"):
        header_cells = row.find_all("th")
        if not header_cells:
            continue
        parsed_header = _parse_essex_header_map(header_cells)
        if parsed_header:
            columns.update(parsed_header)
            break

    rows = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 3:
            continue
        first_cell = _extract_cell_text(cells, columns, "document_type", 0)
        if first_cell == "Type":
            continue
        if not first_cell:
            continue

        recorded_at_raw = _extract_cell_text(cells, columns, "recorded_at", 4)
        consideration_raw = _extract_cell_text(cells, columns, "consideration", 10)
        record = {
            "document_type": first_cell,
            "direct_party": _extract_cell_text(cells, columns, "direct_party", 1),
            "indirect_party": _extract_cell_text(cells, columns, "indirect_party", 2),
            "document_number": _extract_cell_text(cells, columns, "document_number", 3),
            "recorded_at": _parse_date(recorded_at_raw),
            "municipality": _extract_cell_text(cells, columns, "municipality", 5),
            "block": _extract_cell_text(cells, columns, "block", 6),
            "lot": _extract_cell_text(cells, columns, "lot", 7),
            "book": _extract_cell_text(cells, columns, "book", 8) or None,
            "page": _extract_cell_text(cells, columns, "page", 9) or None,
            "consideration": _parse_money(consideration_raw),
        }
        if record["document_number"]:
            rows.append(record)
    return rows


def group_recorder_records(records):
    grouped = {}
    for record in records:
        key = (
            record["document_type"].upper(),
            record["document_number"],
            record["recorded_at"],
            record["municipality"],
            record["block"],
            record["lot"],
        )
        if key not in grouped:
            grouped[key] = {
                "document_type": record["document_type"],
                "document_number": record["document_number"],
                "recorded_at": record["recorded_at"],
                "municipality": record["municipality"],
                "block": record["block"],
                "lot": record["lot"],
                "book": record["book"],
                "page": record["page"],
                "consideration": record["consideration"],
                "document_category": _classifier_document_category(record["document_type"]),
                "grantors": [],
                "grantees": [],
                "rows": [],
            }

        grouped_record = grouped[key]
        grouped_record["rows"].append(record)
        direct_party = record["direct_party"]
        indirect_party = record["indirect_party"]
        if direct_party and direct_party not in grouped_record["grantors"]:
            grouped_record["grantors"].append(direct_party)
        if indirect_party and indirect_party not in grouped_record["grantees"]:
            grouped_record["grantees"].append(indirect_party)
        if grouped_record["book"] in (None, "", "na", "n/a", "&nbsp;") and record["book"] not in (
            None,
            "",
            "na",
            "n/a",
            "&nbsp;",
        ):
            grouped_record["book"] = record["book"]
        if grouped_record["page"] in (None, "", "na", "n/a", "&nbsp;") and record["page"] not in (
            None,
            "",
            "na",
            "n/a",
            "&nbsp;",
        ):
            grouped_record["page"] = record["page"]
        if grouped_record["consideration"] is None and record["consideration"] is not None:
            grouped_record["consideration"] = record["consideration"]

    return list(grouped.values())


def _recorder_sort_key(item):
    recorded_at = item["recorded_at"]
    document_number = item["document_number"]
    normalized_num = re.sub(r"\D", "", document_number)
    numeric_part = int(normalized_num) if normalized_num.isdigit() else -1
    return (recorded_at or datetime.min.date(), numeric_part, document_number)


def select_latest_deed(records):
    if not records:
        return None

    normalized_records = records
    if not all("rows" in record for record in records):
        normalized_records = group_recorder_records(records)

    deed_rows = [row for row in normalized_records if row["document_type"].upper() == DEED_DOCUMENT_TYPE]
    if not deed_rows:
        return None

    latest_record = max(deed_rows, key=_recorder_sort_key)
    latest_rows = latest_record.get("rows", []) or [latest_record]

    grantors = latest_record.get("grantors", [])
    if not grantors:
        grantors = []
        for row in latest_rows:
            if row["direct_party"] and row["direct_party"] not in grantors:
                grantors.append(row["direct_party"])
    grantees = latest_record.get("grantees", [])
    if not grantees:
        grantees = []
        for row in latest_rows:
            if row["indirect_party"] and row["indirect_party"] not in grantees:
                grantees.append(row["indirect_party"])

    consideration = latest_record.get("consideration")
    if consideration is None:
        for row in latest_rows:
            if row.get("consideration") is not None:
                consideration = row["consideration"]
                break

    return {
        "document_type": latest_record["document_type"],
        "document_number": latest_record["document_number"],
        "recorded_at": latest_record["recorded_at"],
        "municipality": latest_record["municipality"],
        "block": latest_record["block"],
        "lot": latest_record["lot"],
        "book": latest_record["book"],
        "page": latest_record["page"],
        "document_category": "deed",
        "grantors": grantors,
        "grantees": grantees,
        "primary_grantee": grantees[0] if grantees else None,
        "consideration": consideration,
        "rows": latest_rows,
    }


def extract_financing_claims(records):
    if not records:
        return []

    normalized_records = records
    if not all("rows" in record for record in records):
        normalized_records = group_recorder_records(records)

    financing_records = []
    for record in normalized_records:
        category = record.get("document_category") or _classifier_document_category(record["document_type"])
        if category not in {"mortgage", "assignment_of_mortgage", "lien", "lis_pendens", "ucc"}:
            continue
        lender_candidates = list(record.get("grantors") or [])
        counterparty_candidates = list(record.get("grantees") or [])
        lender_name = lender_candidates[0] if lender_candidates else (
            counterparty_candidates[0] if counterparty_candidates else "Unknown filing party"
        )
        financing_records.append(
            {
                "document_number": record["document_number"],
                "document_type": record["document_type"],
                "document_category": category,
                "recorded_at": record["recorded_at"],
                "municipality": record["municipality"],
                "block": record["block"],
                "lot": record["lot"],
                "book": record["book"],
                "page": record["page"],
                "consideration": record["consideration"],
                "grantors": lender_candidates,
                "grantees": counterparty_candidates,
                "lender_name": lender_name,
                "rows": record.get("rows", []),
            }
        )

    return sorted(financing_records, key=_recorder_sort_key, reverse=True)


def parse_njbgs_search_results(html):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="js-data-table")
    if not table:
        return []

    rows = []
    for tr in table.find("tbody").find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 5:
            continue
        type_cell = cells[3]
        type_abbr = _normalize_space(type_cell.get_text(" ", strip=True))
        abbr = type_cell.find("abbr")
        rows.append(
            {
                "business_name": _normalize_space(cells[0].get_text(" ", strip=True)),
                "entity_id": _normalize_space(cells[1].get_text(" ", strip=True)),
                "city": _normalize_space(cells[2].get_text(" ", strip=True)) or None,
                "entity_type": type_abbr or None,
                "entity_type_title": abbr.get("title") if abbr else type_abbr or None,
                "formation_date": _parse_date(cells[4].get_text(" ", strip=True)),
            }
        )
    return rows


def parse_njbgs_entity_details(html):
    soup = BeautifulSoup(html, "html.parser")
    details = {"status": None, "registered_agent": None, "officers": []}

    for row in soup.find_all("tr"):
        cells = [_normalize_space(cell.get_text(" ", strip=True)) for cell in row.find_all(["th", "td"])]
        if len(cells) < 2:
            continue
        key = cells[0].rstrip(":").strip().lower()
        value = cells[1]
        if key == "status" and value:
            details["status"] = value
        if key == "registered agent" and value:
            details["registered_agent"] = value

    for table in soup.find_all("table"):
        headers = [_normalize_space(header.get_text(" ", strip=True)).lower() for header in table.find_all("th")]
        if not headers or "name" not in headers or "title" not in headers:
            continue
        if not any(token in " ".join(headers) for token in ("officer", "director", "manager", "title")):
            continue
        name_index = headers.index("name")
        title_index = headers.index("title")
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) <= max(name_index, title_index):
                continue
            officer_name = _normalize_space(cells[name_index].get_text(" ", strip=True))
            officer_title = _normalize_space(cells[title_index].get_text(" ", strip=True))
            if officer_name:
                details["officers"].append({"name": officer_name, "title": officer_title or "Officer"})
    return details


def choose_best_njbgs_match(owner_name, results):
    normalized_owner = normalize_org_name(owner_name)
    scored = []
    for candidate in results:
        normalized_candidate = normalize_org_name(candidate["business_name"])
        scored.append(
            {
                **candidate,
                "normalized_name": normalized_candidate,
                "match_score": round(_sequence_similarity(normalized_owner, normalized_candidate), 4),
            }
        )

    exact_matches = [candidate for candidate in scored if candidate["normalized_name"] == normalized_owner]
    if len(exact_matches) == 1:
        return {"status": "matched", "match": exact_matches[0], "candidates": exact_matches}
    if len(exact_matches) > 1:
        return {"status": "needs_review", "match": None, "candidates": exact_matches}

    strong_matches = sorted(
        [candidate for candidate in scored if candidate["match_score"] >= 0.92],
        key=lambda item: (-item["match_score"], item["business_name"]),
    )
    if len(strong_matches) == 1:
        return {"status": "matched", "match": strong_matches[0], "candidates": strong_matches}
    if len(strong_matches) > 1:
        return {"status": "needs_review", "match": None, "candidates": strong_matches[:5]}

    review_matches = sorted(
        [candidate for candidate in scored if candidate["match_score"] >= 0.80],
        key=lambda item: (-item["match_score"], item["business_name"]),
    )
    if review_matches:
        return {"status": "needs_review", "match": None, "candidates": review_matches[:5]}

    return {"status": "not_found", "match": None, "candidates": []}


class EssexRecorderClient:
    def __init__(self, base_url, cache_dir, request_delay_seconds=0.0, fixtures_dir=None):
        self.base_url = base_url
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.request_delay_seconds = request_delay_seconds
        self.fixtures_dir = Path(fixtures_dir) if fixtures_dir else None
        self.session = requests.Session()

    def _fixture_path(self, block, lot):
        if not self.fixtures_dir:
            return None
        return self.fixtures_dir / ("essex-block-%s-lot-%s.html" % (_slugify(block), _slugify(lot)))

    def _searchable_html_paths(self):
        paths = []
        if self.fixtures_dir and self.fixtures_dir.exists():
            paths.extend(sorted(self.fixtures_dir.glob("essex-block-*.html")))
        if self.cache_dir.exists():
            paths.extend(sorted(self.cache_dir.glob("essex-*.html")))
        deduped = []
        seen = set()
        for path in paths:
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            deduped.append(path)
        return deduped

    def search_parcel(self, municipality, block, lot, force_refresh=False):
        fixture_path = self._fixture_path(block, lot)
        if fixture_path and fixture_path.exists():
            return fixture_path.read_text(), fixture_path

        cache_path = self.cache_dir / ("essex-%s-block-%s-lot-%s.html" % (_slugify(municipality), _slugify(block), _slugify(lot)))
        if cache_path.exists() and not force_refresh:
            return cache_path.read_text(), cache_path

        initial = self.session.get(self.base_url, timeout=30)
        initial.raise_for_status()
        soup = BeautifulSoup(initial.text, "html.parser")
        form = soup.find("form")
        payload = _build_form_payload(form)
        payload.update(
            {
                "ctl00$ContentPlaceHolder1$ddlMunTab4": municipality,
                "ctl00$ContentPlaceHolder1$txtBlockTab4": block,
                "ctl00$ContentPlaceHolder1$txtLotTab4": lot,
                "ctl00$ContentPlaceHolder1$ddlShowRecTab4": "20",
                "ctl00$ContentPlaceHolder1$ddlTotalRecTab4": "100",
                "ctl00$ContentPlaceHolder1$btnSearchTab4": "Search",
                "ctl00$ContentPlaceHolder1$hidDiv": "basic",
                "hidCurrTab": "4",
            }
        )
        response = self.session.post(urljoin(initial.url, form.get("action")), data=payload, timeout=30)
        response.raise_for_status()
        cache_path.write_text(response.text)
        if self.request_delay_seconds > 0:
            time.sleep(self.request_delay_seconds)
        return response.text, cache_path

    def search_name(self, name, force_refresh=False):
        normalized_name = normalize_org_name(name)
        for path in self._searchable_html_paths():
            html = path.read_text()
            records = parse_essex_search_results(html)
            for record in records:
                parties = [record.get("direct_party"), record.get("indirect_party")]
                if any(normalize_org_name(party) == normalized_name for party in parties if party):
                    return html, path
        return "", None


class NJBusinessSearchClient:
    def __init__(self, base_url, cache_dir, request_delay_seconds=0.0, fixtures_dir=None):
        self.base_url = base_url
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.request_delay_seconds = request_delay_seconds
        self.fixtures_dir = Path(fixtures_dir) if fixtures_dir else None
        self.session = requests.Session()

    def _fixture_path(self, name):
        if not self.fixtures_dir:
            return None
        return self.fixtures_dir / ("njbgs-%s.html" % _slugify(name))

    def search_name(self, name, force_refresh=False):
        fixture_path = self._fixture_path(name)
        if fixture_path and fixture_path.exists():
            return fixture_path.read_text(), fixture_path

        cache_path = self.cache_dir / ("njbgs-%s.html" % _slugify(name))
        if cache_path.exists() and not force_refresh:
            return cache_path.read_text(), cache_path

        initial = self.session.get(self.base_url, timeout=30)
        initial.raise_for_status()
        soup = BeautifulSoup(initial.text, "html.parser")
        form = soup.find("form")
        payload = _build_form_payload(form)
        payload.update({"BusinessName": name})
        response = self.session.post(urljoin(initial.url, form.get("action")), data=payload, timeout=30)
        response.raise_for_status()
        cache_path.write_text(response.text)
        if self.request_delay_seconds > 0:
            time.sleep(self.request_delay_seconds)
        return response.text, cache_path


def _upsert_source_document(cursor, document):
    cursor.execute(
        """
        INSERT INTO source_documents (
            id, source_name, title, source_url, access_date, document_type, parser_version
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            source_name = EXCLUDED.source_name,
            title = EXCLUDED.title,
            source_url = EXCLUDED.source_url,
            access_date = EXCLUDED.access_date,
            document_type = EXCLUDED.document_type,
            parser_version = EXCLUDED.parser_version
        """,
        (
            document["id"],
            document["source_name"],
            document["title"],
            document["source_url"],
            document["access_date"],
            document["document_type"],
            document["parser_version"],
        ),
    )


def _fetch_parcel(cursor, market_id, parcel_id):
    cursor.execute(
        """
        SELECT id, market_id, parcel_pin, block, lot, property_location,
               ownership_enrichment_status, ownership_last_enriched_at
        FROM parcels
        WHERE market_id = %s AND id = %s
        """,
        (market_id, parcel_id),
    )
    return cursor.fetchone()


def _mark_parcel_status(cursor, parcel_id, status, now, note=None, metadata=None):
    cursor.execute(
        """
        UPDATE parcels
        SET ownership_enrichment_status = %s,
            ownership_last_enriched_at = %s,
            ownership_note = %s,
            ownership_metadata = %s
        WHERE id = %s
        """,
        (status, now, note, Json(_json_ready(metadata or {})), parcel_id),
    )


def _ensure_recorder_document(cursor, parcel_id, latest_deed, source_document_id, now):
    record_id = "recorder-%s-%s" % (_slugify(parcel_id), _slugify(latest_deed["document_number"]))
    cursor.execute(
        """
        INSERT INTO recorder_documents (
            id, parcel_id, document_number, document_type, recorded_at, municipality, block, lot,
            book, page, document_category, grantors, grantees, consideration, source_document_id, last_enriched_at, raw_payload
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (parcel_id, document_number, recorded_at) DO UPDATE SET
            document_type = EXCLUDED.document_type,
            municipality = EXCLUDED.municipality,
            block = EXCLUDED.block,
            lot = EXCLUDED.lot,
            book = EXCLUDED.book,
            page = EXCLUDED.page,
            document_category = EXCLUDED.document_category,
            grantors = EXCLUDED.grantors,
            grantees = EXCLUDED.grantees,
            consideration = EXCLUDED.consideration,
            source_document_id = EXCLUDED.source_document_id,
            last_enriched_at = EXCLUDED.last_enriched_at,
            raw_payload = EXCLUDED.raw_payload
        RETURNING id
        """,
        (
            record_id,
            parcel_id,
            latest_deed["document_number"],
            latest_deed["document_type"],
            latest_deed["recorded_at"],
            latest_deed["municipality"],
            latest_deed["block"],
            latest_deed["lot"],
            latest_deed["book"],
            latest_deed["page"],
            latest_deed.get("document_category")
            or _classifier_document_category(latest_deed["document_type"]),
            Json(latest_deed["grantors"]),
            Json(latest_deed["grantees"]),
            latest_deed["consideration"],
            source_document_id,
            now,
            Json(
                _json_ready(
                    {
                    "grantors": latest_deed["grantors"],
                    "grantees": latest_deed["grantees"],
                    "rows": latest_deed["rows"],
                    }
                )
            ),
        ),
    )
    return cursor.fetchone()["id"]


def _ensure_exact_named_organization(cursor, market_id, organization_name, source_document_id, now):
    normalized_name = normalize_org_name(organization_name)
    if not normalized_name:
        return None

    cursor.execute(
        """
        SELECT id
        FROM organizations
        WHERE market_id = %s
          AND normalized_name = %s
        ORDER BY (nj_entity_id IS NOT NULL) DESC, last_enriched_at DESC NULLS LAST, id
        LIMIT 1
        """,
        (market_id, normalized_name),
    )
    existing = cursor.fetchone()
    organization_id = (
        existing["id"]
        if existing
        else "organization-recorder-%s-%s"
        % (_slugify(market_id), _slugify(normalized_name or organization_name))
    )
    cursor.execute(
        """
        INSERT INTO organizations (
            id, market_id, name, normalized_name, source_document_id, last_enriched_at, raw_payload
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            name = EXCLUDED.name,
            normalized_name = EXCLUDED.normalized_name,
            source_document_id = EXCLUDED.source_document_id,
            last_enriched_at = EXCLUDED.last_enriched_at,
            raw_payload = organizations.raw_payload || EXCLUDED.raw_payload
        RETURNING id
        """,
        (
            organization_id,
            market_id,
            organization_name,
            normalized_name,
            source_document_id,
            now,
            Json(
                _json_ready(
                    {
                        "source": "recorder_document",
                        "organization_name": organization_name,
                    }
                )
            ),
        ),
    )
    return cursor.fetchone()["id"]


def _ensure_organization(cursor, market_id, matched_entity, details, source_document_id, now):
    organization_id = "organization-nj-%s" % _slugify(matched_entity["entity_id"])
    cursor.execute(
        """
        INSERT INTO organizations (
            id, market_id, name, normalized_name, nj_entity_id, matched_search_name, domicile_city,
            entity_type, entity_type_title, status, formation_date, registered_agent,
            source_document_id, last_enriched_at, raw_payload
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            name = EXCLUDED.name,
            normalized_name = EXCLUDED.normalized_name,
            nj_entity_id = EXCLUDED.nj_entity_id,
            matched_search_name = EXCLUDED.matched_search_name,
            domicile_city = EXCLUDED.domicile_city,
            entity_type = EXCLUDED.entity_type,
            entity_type_title = EXCLUDED.entity_type_title,
            status = EXCLUDED.status,
            formation_date = EXCLUDED.formation_date,
            registered_agent = EXCLUDED.registered_agent,
            source_document_id = EXCLUDED.source_document_id,
            last_enriched_at = EXCLUDED.last_enriched_at,
            raw_payload = EXCLUDED.raw_payload
        RETURNING id
        """,
        (
            organization_id,
            market_id,
            matched_entity["business_name"],
            normalize_org_name(matched_entity["business_name"]),
            matched_entity["entity_id"],
            matched_entity["business_name"],
            matched_entity.get("city"),
            matched_entity.get("entity_type"),
            matched_entity.get("entity_type_title"),
            details.get("status"),
            matched_entity.get("formation_date"),
            details.get("registered_agent"),
            source_document_id,
            now,
            Json(_json_ready({"search_result": matched_entity, "details": details})),
        ),
    )
    return cursor.fetchone()["id"]


def _replace_organization_roles(cursor, organization_id, officers, source_document_id, now):
    cursor.execute("DELETE FROM organization_roles WHERE organization_id = %s", (organization_id,))
    for officer in officers:
        person_id = "person-%s-%s" % (_slugify(organization_id), _slugify(officer["name"]))
        cursor.execute(
            """
            INSERT INTO people (id, full_name, normalized_name, source_document_id, last_enriched_at, raw_payload)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                full_name = EXCLUDED.full_name,
                normalized_name = EXCLUDED.normalized_name,
                source_document_id = EXCLUDED.source_document_id,
                last_enriched_at = EXCLUDED.last_enriched_at,
                raw_payload = EXCLUDED.raw_payload
            """,
            (
                person_id,
                officer["name"],
                normalize_org_name(officer["name"]),
                source_document_id,
                now,
                Json(_json_ready(officer)),
            ),
        )
        role_id = "role-%s-%s-%s" % (
            _slugify(organization_id),
            _slugify(officer["name"]),
            _slugify(officer["title"]),
        )
        cursor.execute(
            """
            INSERT INTO organization_roles (
                id, organization_id, person_id, role_title, role_type, source_document_id, last_enriched_at, raw_payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (organization_id, person_id, role_title) DO UPDATE SET
                source_document_id = EXCLUDED.source_document_id,
                last_enriched_at = EXCLUDED.last_enriched_at,
                raw_payload = EXCLUDED.raw_payload
            """,
            (
                role_id,
                organization_id,
                person_id,
                officer["title"],
                "officer",
                source_document_id,
                now,
                Json(_json_ready(officer)),
            ),
        )


def _upsert_claim(cursor, parcel_id, owner_name, organization_id, recorder_document_id, source_document_id, notes, now, valid_from):
    claim_id = "claim-%s-%s-%s" % (
        _slugify(parcel_id),
        _slugify(recorder_document_id),
        _slugify(owner_name),
    )
    cursor.execute(
        """
        SELECT id, recorder_document_id, owner_name
        FROM parcel_ownership_claims
        WHERE parcel_id = %s
          AND valid_to IS NULL
        ORDER BY COALESCE(valid_from, DATE '1900-01-01') DESC, created_at DESC
        LIMIT 1
        """,
        (parcel_id,),
    )
    current_claim = cursor.fetchone()

    if current_claim and current_claim["recorder_document_id"] == recorder_document_id and current_claim["owner_name"] == owner_name:
        cursor.execute(
            """
            UPDATE parcel_ownership_claims
            SET organization_id = %s,
                source_document_id = %s,
                notes = %s,
                last_enriched_at = %s
            WHERE id = %s
            """,
            (organization_id, source_document_id, notes, now, current_claim["id"]),
        )
        return current_claim["id"]

    cursor.execute(
        """
        INSERT INTO parcel_ownership_claims (
            id, parcel_id, organization_id, recorder_document_id, owner_name, confidence_tier,
            valid_from, source_document_id, last_enriched_at, notes
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (parcel_id, recorder_document_id, owner_name) DO UPDATE SET
            organization_id = EXCLUDED.organization_id,
            source_document_id = EXCLUDED.source_document_id,
            last_enriched_at = EXCLUDED.last_enriched_at,
            notes = EXCLUDED.notes
        RETURNING id
        """,
        (
            claim_id,
            parcel_id,
            organization_id,
            recorder_document_id,
            owner_name,
            CONFIDENCE_PROBABLE,
            valid_from,
            source_document_id,
            now,
            notes,
        ),
    )
    new_claim_id = cursor.fetchone()["id"]
    if current_claim and current_claim["id"] != new_claim_id:
        cursor.execute(
            """
            UPDATE parcel_ownership_claims
            SET valid_to = %s,
                superseded_by = %s,
                last_enriched_at = %s
            WHERE id = %s
            """,
            (valid_from, new_claim_id, now, current_claim["id"]),
        )
    return new_claim_id


def _upsert_financing_claim(
    cursor,
    parcel_id,
    organization_id,
    lender_name,
    recorder_document_id,
    claim_type,
    amount,
    source_document_id,
    notes,
    now,
    valid_from,
):
    claim_id = "financing-%s-%s-%s-%s" % (
        _slugify(parcel_id),
        _slugify(recorder_document_id),
        _slugify(lender_name),
        _slugify(claim_type),
    )
    cursor.execute(
        """
        INSERT INTO parcel_financing_claims (
            id,
            parcel_id,
            organization_id,
            lender_name,
            recorder_document_id,
            claim_type,
            amount,
            confidence_tier,
            valid_from,
            source_document_id,
            last_enriched_at,
            notes
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (parcel_id, recorder_document_id, lender_name, claim_type) DO UPDATE SET
            organization_id = EXCLUDED.organization_id,
            amount = EXCLUDED.amount,
            source_document_id = EXCLUDED.source_document_id,
            last_enriched_at = EXCLUDED.last_enriched_at,
            notes = EXCLUDED.notes
        RETURNING id
        """,
        (
            claim_id,
            parcel_id,
            organization_id,
            lender_name,
            recorder_document_id,
            claim_type,
            amount,
            CONFIDENCE_PROBABLE,
            valid_from,
            source_document_id,
            now,
            notes,
        ),
    )
    return cursor.fetchone()["id"]


def enrich_parcel_ownership(connection, market_id, parcel_id, recorder_client, business_client, force_refresh=False):
    from app.ownership_service import mark_parcel_failed, mark_parcel_in_progress
    from app.ownership_tools import BusinessRegistryTool, RecorderTool
    from app.ownership_workflow import run_ownership_workflow

    ensure_schema(connection)
    parcel = mark_parcel_in_progress(connection, market_id, parcel_id)
    if not parcel:
        return None

    try:
        final_state = run_ownership_workflow(
            connection,
            market_id,
            parcel_id,
            recorder_tool=RecorderTool(recorder_client),
            business_tool=BusinessRegistryTool(business_client),
            tracker=None,
            force_refresh=force_refresh,
        )
        return final_state.get("persistence_result", {"parcel_id": parcel_id, "enrichment_status": STATUS_COMPLETED})
    except Exception as exc:
        connection.rollback()
        mark_parcel_failed(connection, market_id, parcel_id, str(exc))
        return {
            "parcel_id": parcel_id,
            "enrichment_status": STATUS_FAILED,
            "error": str(exc),
            "review_reasons": ["ownership enrichment failed during extraction and should be retried"],
            "qa": {
                "issues": [_qa_reason_payload("failed", "ownership enrichment failed", "enrichment_exception")],
                "warnings": [],
                "active_claim_count": 0,
                "active_claim_ids": [],
            },
        }


def enrich_newark_ownership(top_n=None, parcel_id=None, fixtures_dir=None, force_refresh=False, progress=None):
    settings = get_settings()
    now = _utc_now()
    cache_dir = settings.raw_dir / "ownership"
    recorder_client = EssexRecorderClient(
        base_url=settings.essex_recorder_url,
        cache_dir=cache_dir,
        request_delay_seconds=0.0 if fixtures_dir else settings.ownership_request_delay_seconds,
        fixtures_dir=fixtures_dir,
    )
    business_client = NJBusinessSearchClient(
        base_url=settings.nj_business_search_url,
        cache_dir=cache_dir,
        request_delay_seconds=0.0 if fixtures_dir else settings.ownership_request_delay_seconds,
        fixtures_dir=fixtures_dir,
    )

    connection = get_connection(settings.database_url)
    parcel_ids = []
    results = []
    try:
        with advisory_job_lock(connection, LOCK_NAMESPACE_OWNERSHIP, settings.market_id):
            _log_step(progress, "Ownership job lock acquired")
            ensure_schema(connection)

            with connection.cursor() as cursor:
                if parcel_id:
                    cursor.execute(
                        "SELECT id FROM parcels WHERE market_id = %s AND id = %s",
                        (settings.market_id, parcel_id),
                    )
                else:
                    if top_n is None:
                        cursor.execute(
                            """
                            SELECT id
                            FROM parcels
                            WHERE market_id = %s
                              AND priority_tier = 'Tier 1'
                            ORDER BY priority_score DESC NULLS LAST, total_assessed_value DESC, id
                            """,
                            (settings.market_id,),
                        )
                    else:
                        cursor.execute(
                            """
                            SELECT id
                            FROM parcels
                            WHERE market_id = %s
                              AND priority_tier = 'Tier 1'
                            ORDER BY priority_score DESC NULLS LAST, total_assessed_value DESC, id
                            LIMIT %s
                            """,
                            (settings.market_id, top_n),
                        )
                parcel_ids = [row["id"] for row in cursor.fetchall()]

            _log_step(progress, "Ownership target parcels: %s" % len(parcel_ids))
            for index, selected_parcel_id in enumerate(parcel_ids, start=1):
                _log_step(progress, "Enriching ownership %s/%s: %s" % (index, len(parcel_ids), selected_parcel_id))
                results.append(
                    enrich_parcel_ownership(
                        connection,
                        settings.market_id,
                        selected_parcel_id,
                        recorder_client,
                        business_client,
                        force_refresh=force_refresh,
                    )
                )
            _log_step(progress, "Ownership enrichment completed")
    finally:
        connection.close()
        _log_step(progress, "Ownership DB connection closed")

    status_counts = {}
    qa_counts = {
        "issues": 0,
        "warnings": 0,
        "reasons": {},
        "parcels_needing_review": 0,
        "parcels_failed": 0,
    }
    for result in results:
        status = result["enrichment_status"]
        status_counts[status] = status_counts.get(status, 0) + 1
        if status == STATUS_FAILED:
            qa_counts["parcels_failed"] += 1
        if status == STATUS_NEEDS_REVIEW:
            qa_counts["parcels_needing_review"] += 1
        for reason in result.get("review_reasons", []):
            qa_counts["reasons"][reason] = qa_counts["reasons"].get(reason, 0) + 1
        qa = result.get("qa", {})
        for issue in qa.get("issues", []):
            qa_counts["issues"] += 1
        for warning in qa.get("warnings", []):
            qa_counts["warnings"] += 1

    return {
        "run_at": now.isoformat(),
        "market_id": settings.market_id,
        "parcel_count": len(parcel_ids),
        "status_counts": status_counts,
        "qa_counts": qa_counts,
        "results": results,
    }
