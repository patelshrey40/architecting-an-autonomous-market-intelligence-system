from __future__ import annotations

from dataclasses import asdict, dataclass

from app.ownership_service import load_parcel_context

from app.ownership import (
    EssexRecorderClient,
    NJBusinessSearchClient,
    _cache_access_date,
    choose_best_njbgs_match,
    group_recorder_records,
    normalize_org_name,
    parse_essex_search_results,
    parse_njbgs_entity_details,
    parse_njbgs_search_results,
    select_latest_deed,
)


@dataclass(frozen=True)
class ParcelReference:
    parcel_id: str
    municipality: str
    block: str
    lot: str


@dataclass(frozen=True)
class LatestDeedResult:
    source_name: str
    source_url: str
    access_date: str
    municipality: str
    block: str
    lot: str
    grouped_records: list[dict]
    latest_deed: dict | None

    def to_state(self):
        return asdict(self)


@dataclass(frozen=True)
class RecorderPartySearchResult:
    source_name: str
    source_url: str
    access_date: str
    party_name: str
    grouped_records: list[dict]

    def to_state(self):
        return asdict(self)


@dataclass(frozen=True)
class RegistrySearchResult:
    source_name: str
    source_url: str
    access_date: str
    searched_name: str
    candidates: list[dict]

    def to_state(self):
        return asdict(self)


@dataclass(frozen=True)
class EntityMatchResult:
    source_name: str
    source_url: str
    access_date: str
    owner_name: str
    match_status: str
    matched_entity: dict | None
    candidates: list[dict]
    details: dict

    def to_state(self):
        return asdict(self)


class RecorderTool:
    def __init__(self, client: EssexRecorderClient):
        self.client = client

    def fetch_recorder_by_block_lot(self, parcel_ref: ParcelReference, force_refresh=False) -> LatestDeedResult:
        html, cache_path = self.client.search_parcel(
            parcel_ref.municipality,
            parcel_ref.block,
            parcel_ref.lot,
            force_refresh=force_refresh,
        )
        records = parse_essex_search_results(html)
        grouped_records = group_recorder_records(records)
        latest_deed = select_latest_deed(records)
        return LatestDeedResult(
            source_name="essex_county_recorder",
            source_url=self.client.base_url,
            access_date=_cache_access_date(cache_path),
            municipality=parcel_ref.municipality,
            block=parcel_ref.block,
            lot=parcel_ref.lot,
            grouped_records=grouped_records,
            latest_deed=latest_deed,
        )

    def fetch_latest_deed(self, parcel_ref: ParcelReference, force_refresh=False) -> LatestDeedResult:
        return self.fetch_recorder_by_block_lot(parcel_ref, force_refresh=force_refresh)

    def search_recorder_by_party_name(self, name: str, force_refresh=False) -> RecorderPartySearchResult:
        html, cache_path = self.client.search_name(name, force_refresh=force_refresh)
        records = parse_essex_search_results(html) if html else []
        filtered_records = []
        normalized_party = normalize_org_name(name)
        for record in records:
            parties = [record.get("direct_party"), record.get("indirect_party")]
            if any(normalize_org_name(party) == normalized_party for party in parties if party):
                filtered_records.append(record)
        grouped_records = group_recorder_records(filtered_records)
        return RecorderPartySearchResult(
            source_name="essex_county_recorder_party_search",
            source_url=self.client.base_url,
            access_date=_cache_access_date(cache_path),
            party_name=name,
            grouped_records=grouped_records,
        )


class BusinessRegistryTool:
    def __init__(self, client: NJBusinessSearchClient):
        self.client = client

    def search_njbgs_by_name(self, name: str, force_refresh=False) -> RegistrySearchResult:
        html, cache_path = self.client.search_name(name, force_refresh=force_refresh)
        results = parse_njbgs_search_results(html)
        return RegistrySearchResult(
            source_name="nj_business_name_search",
            source_url=self.client.base_url,
            access_date=_cache_access_date(cache_path),
            searched_name=name,
            candidates=[
                {
                    "business_name": candidate["business_name"],
                    "entity_id": candidate["entity_id"],
                    "city": candidate["city"],
                    "entity_type": candidate["entity_type"],
                    "entity_type_title": candidate["entity_type_title"],
                    "formation_date": candidate["formation_date"],
                }
                for candidate in results
            ],
        )

    def get_njbgs_entity_details(self, name: str, force_refresh=False) -> dict:
        html, _cache_path = self.client.search_name(name, force_refresh=force_refresh)
        return parse_njbgs_entity_details(html)

    def match_owner(self, name: str, force_refresh=False) -> EntityMatchResult:
        html, cache_path = self.client.search_name(name, force_refresh=force_refresh)
        results = parse_njbgs_search_results(html)
        match = choose_best_njbgs_match(name, results)
        details = parse_njbgs_entity_details(html)
        return EntityMatchResult(
            source_name="nj_business_name_search",
            source_url=self.client.base_url,
            access_date=_cache_access_date(cache_path),
            owner_name=name,
            match_status=match["status"],
            matched_entity=match["match"],
            candidates=[
                {
                    "business_name": candidate["business_name"],
                    "entity_id": candidate["entity_id"],
                    "match_score": candidate["match_score"],
                }
                for candidate in match["candidates"][:5]
            ],
            details=details,
        )


def load_existing_parcel_context_tool(connection, market_id: str, parcel_id: str) -> dict | None:
    return load_parcel_context(connection, market_id, parcel_id)


def load_existing_org_context_tool(connection, market_id: str, name: str) -> list[dict]:
    normalized_name = normalize_org_name(name)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                id,
                name,
                normalized_name,
                nj_entity_id,
                matched_search_name,
                domicile_city,
                entity_type,
                entity_type_title,
                status,
                formation_date,
                registered_agent,
                source_document_id,
                last_enriched_at
            FROM organizations
            WHERE market_id = %s
              AND normalized_name = %s
            ORDER BY last_enriched_at DESC NULLS LAST, id
            """,
            (market_id, normalized_name),
        )
        rows = cursor.fetchall()
    return [
        {
            **dict(row),
            "formation_date": row["formation_date"].isoformat() if row["formation_date"] else None,
            "last_enriched_at": row["last_enriched_at"].isoformat() if row["last_enriched_at"] else None,
        }
        for row in rows
    ]
