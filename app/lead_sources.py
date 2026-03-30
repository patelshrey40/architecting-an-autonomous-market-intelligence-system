from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import time
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
import requests

from app.ownership import _cache_access_date, _normalize_space


DEFAULT_NEWARK_LEAD_SOURCES = [
    {
        "id": "newark-planning",
        "source_family": "civic_roster",
        "persona_hint": "planner",
        "title": "Newark Planning Department",
        "url": "https://www.newarknj.gov/departments/planning",
        "organization_name": "City of Newark Planning Department",
    },
    {
        "id": "newark-zoning-board",
        "source_family": "civic_roster",
        "persona_hint": "planner",
        "title": "Newark Zoning Board of Adjustment",
        "url": "https://www.newarknj.gov/card/zoning-board-of-adjustment",
        "organization_name": "City of Newark Zoning Board of Adjustment",
    },
    {
        "id": "newark-city-clerk",
        "source_family": "civic_roster",
        "persona_hint": "planner",
        "title": "Newark City Clerk",
        "url": "https://www.newarknj.gov/departments/city-clerk",
        "organization_name": "City of Newark City Clerk",
    },
]


@dataclass(frozen=True)
class LeadSourceDefinition:
    id: str
    source_family: str
    persona_hint: str
    title: str
    url: str
    organization_name: str | None = None


@dataclass(frozen=True)
class LeadSourceFetchResult:
    source: LeadSourceDefinition
    source_name: str
    source_url: str
    access_date: str
    source_document_title: str
    contacts: list[dict]
    people: list[dict]

    def to_state(self):
        return asdict(self)


class PublicHtmlClient:
    def __init__(self, cache_dir: Path, request_delay_seconds=0.5, fixtures_dir: Path | None = None):
        self.cache_dir = cache_dir
        self.request_delay_seconds = request_delay_seconds
        self.fixtures_dir = fixtures_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, source_id: str):
        return self.cache_dir / ("%s.html" % source_id)

    def fetch(self, source: LeadSourceDefinition, force_refresh=False):
        cache_path = self._cache_path(source.id)
        if self.fixtures_dir:
            fixture_path = self.fixtures_dir / ("%s.html" % source.id)
            return fixture_path.read_text(), fixture_path
        if cache_path.exists() and not force_refresh:
            return cache_path.read_text(), cache_path
        response = requests.get(source.url, timeout=30)
        response.raise_for_status()
        cache_path.write_text(response.text)
        if self.request_delay_seconds:
            time.sleep(self.request_delay_seconds)
        return response.text, cache_path


class PublicRosterAdapter:
    def __init__(self, client: PublicHtmlClient, provider):
        self.client = client
        self.provider = provider

    def fetch_people(self, source: LeadSourceDefinition, force_refresh=False):
        html, cache_path = self.client.fetch(source, force_refresh=force_refresh)
        fallback = heuristic_extract_people(source, html)
        schema = {
            "type": "object",
            "properties": {
                "people": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "full_name": {"type": "string"},
                            "title": {"type": "string"},
                            "email": {"type": ["string", "null"]},
                            "phone": {"type": ["string", "null"]},
                            "profile_url": {"type": ["string", "null"]},
                            "office_address": {"type": ["string", "null"]},
                            "organization_name": {"type": ["string", "null"]},
                        },
                        "required": ["full_name", "title", "organization_name"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["people"],
            "additionalProperties": False,
        }
        text_content = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)[:12000]
        extracted = self.provider.generate_structured(
            "roster_extractor",
            schema,
            {
                "instructions": (
                    "Extract real people from this public civic or firm source. "
                    "Do not invent names or contact details. "
                    "If only a page-level contact exists, attach it only when clearly connected to the person role."
                ),
                "input": "Source title: %s\nSource family: %s\nContent:\n%s"
                % (source.title, source.source_family, text_content),
                "fallback": {"people": fallback["people"]},
            },
        )
        return LeadSourceFetchResult(
            source=source,
            source_name=source.source_family,
            source_url=source.url,
            access_date=_cache_access_date(cache_path),
            source_document_title=source.title,
            contacts=fallback["contacts"],
            people=extracted.get("people", []),
        )


def load_lead_source_definitions(settings):
    sources = [LeadSourceDefinition(**item) for item in DEFAULT_NEWARK_LEAD_SOURCES]
    if settings.lead_source_config_path and settings.lead_source_config_path.exists():
        extra_items = json.loads(settings.lead_source_config_path.read_text())
        sources.extend(LeadSourceDefinition(**item) for item in extra_items)
    return sources


def heuristic_extract_people(source: LeadSourceDefinition, html: str):
    soup = BeautifulSoup(html, "html.parser")
    page_contacts = extract_page_contacts(soup, source.url)
    people = []
    seen = set()

    for container in soup.select("article, li, .card, .member, .team-member, .staff, .person, section, div"):
        name = None
        title = None
        for selector in ("h1", "h2", "h3", "h4", "strong"):
            node = container.select_one(selector)
            if node:
                candidate = _normalize_space(node.get_text(" ", strip=True))
                if _looks_like_person_name(candidate):
                    name = candidate
                    break
        if not name:
            continue
        texts = [
            _normalize_space(node.get_text(" ", strip=True))
            for node in container.find_all(["p", "span", "div", "small"], recursive=False)
        ]
        for candidate in texts:
            if candidate and candidate != name and len(candidate.split()) <= 12:
                title = candidate
                break
        emails = _extract_emails(container)
        phones = _extract_phones(container)
        profile_url = None
        anchor = container.find("a", href=True)
        if anchor:
            profile_url = urljoin(source.url, anchor["href"])
        entry = {
            "full_name": name,
            "title": title or source.title,
            "email": emails[0] if emails else (page_contacts["emails"][0] if page_contacts["emails"] else None),
            "phone": phones[0] if phones else (page_contacts["phones"][0] if page_contacts["phones"] else None),
            "profile_url": profile_url or source.url,
            "office_address": page_contacts["addresses"][0] if page_contacts["addresses"] else None,
            "organization_name": source.organization_name or source.title,
        }
        key = (entry["full_name"].lower(), entry["title"].lower())
        if key in seen:
            continue
        seen.add(key)
        people.append(entry)

    if not people:
        line_candidates = []
        for line in soup.get_text("\n", strip=True).splitlines():
            clean = _normalize_space(line)
            if not clean:
                continue
            if " - " in clean:
                line_candidates.append(clean)
            elif "," in clean and _looks_like_person_name(clean.split(",")[0]):
                line_candidates.append(clean)
        for line in line_candidates[:20]:
            if " - " in line:
                left, right = [part.strip() for part in line.split(" - ", 1)]
            else:
                left, right = [part.strip() for part in line.split(",", 1)]
            if not _looks_like_person_name(left):
                continue
            people.append(
                {
                    "full_name": left,
                    "title": right or source.title,
                    "email": page_contacts["emails"][0] if page_contacts["emails"] else None,
                    "phone": page_contacts["phones"][0] if page_contacts["phones"] else None,
                    "profile_url": source.url,
                    "office_address": page_contacts["addresses"][0] if page_contacts["addresses"] else None,
                    "organization_name": source.organization_name or source.title,
                }
            )

    return {"people": people[:20], "contacts": page_contacts}


def extract_page_contacts(soup, page_url):
    emails = _extract_emails(soup)
    phones = _extract_phones(soup)
    addresses = []
    for node in soup.find_all(["address", "p", "div"]):
        text = _normalize_space(node.get_text(" ", strip=True))
        if _looks_like_address(text):
            addresses.append(text)
    deduped_addresses = []
    for address in addresses:
        if address not in deduped_addresses:
            deduped_addresses.append(address)
    return {
        "emails": emails[:3],
        "phones": phones[:3],
        "addresses": deduped_addresses[:2],
        "profile_url": page_url,
    }


def _extract_emails(node):
    emails = []
    for anchor in node.find_all("a", href=True):
        href = anchor["href"]
        if href.lower().startswith("mailto:"):
            email = href.split(":", 1)[1].split("?", 1)[0].strip()
            if email and email not in emails:
                emails.append(email)
    for match in re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", node.get_text(" ", strip=True), flags=re.I):
        if match not in emails:
            emails.append(match)
    return emails


def _extract_phones(node):
    phones = []
    for anchor in node.find_all("a", href=True):
        href = anchor["href"]
        if href.lower().startswith("tel:"):
            phone = href.split(":", 1)[1].strip()
            if phone and phone not in phones:
                phones.append(phone)
    for match in re.findall(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}", node.get_text(" ", strip=True)):
        clean = _normalize_space(match)
        if clean not in phones:
            phones.append(clean)
    return phones


def _looks_like_person_name(value):
    if not value:
        return False
    if len(value.split()) < 2 or len(value.split()) > 5:
        return False
    if any(token.isdigit() for token in value.split()):
        return False
    if len(value) > 64:
        return False
    stop_tokens = {"contact", "office", "department", "planning", "board", "city", "newark"}
    parts = [item.lower().strip(",") for item in value.split()]
    if any(part in stop_tokens for part in parts):
        return False
    return all(part[:1].isalpha() for part in parts)


def _looks_like_address(value):
    if not value:
        return False
    lower = value.lower()
    if not any(token in lower for token in ("street", "st", "avenue", "ave", "road", "rd", "blvd", "boulevard")):
        return False
    return bool(re.search(r"\d{2,5}", value))
