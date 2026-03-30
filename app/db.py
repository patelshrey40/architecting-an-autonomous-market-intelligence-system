from contextlib import contextmanager
from pathlib import Path
import time
import zlib

import psycopg
from psycopg.rows import dict_row


SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema" / "postgres.sql"
LOCK_NAMESPACE_INGEST = 1001
LOCK_NAMESPACE_OWNERSHIP = 1002
_SCHEMA_READY = False

RUNTIME_MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS redevelopment_areas (
        id TEXT PRIMARY KEY,
        market_id TEXT NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        short_name TEXT,
        plan_link TEXT,
        source_document_id TEXT REFERENCES source_documents(id),
        last_ingested_at TIMESTAMPTZ,
        geom geometry(MultiPolygon, 4326) NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS redevelopment_areas_geom_idx ON redevelopment_areas USING gist (geom)",
    "CREATE INDEX IF NOT EXISTS redevelopment_areas_market_idx ON redevelopment_areas (market_id)",
    """
    CREATE TABLE IF NOT EXISTS transit_stops (
        id TEXT PRIMARY KEY,
        market_id TEXT NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
        stop_name TEXT NOT NULL,
        stop_type TEXT NOT NULL,
        rail_line TEXT,
        municipality TEXT,
        county TEXT,
        source_document_id TEXT REFERENCES source_documents(id),
        last_ingested_at TIMESTAMPTZ,
        geom geometry(Point, 4326) NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS transit_stops_geom_idx ON transit_stops USING gist (geom)",
    "CREATE INDEX IF NOT EXISTS transit_stops_market_idx ON transit_stops (market_id, stop_type)",
    """
    CREATE TABLE IF NOT EXISTS agent_runs (
        id TEXT PRIMARY KEY,
        market_id TEXT NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        workflow_type TEXT NOT NULL,
        status TEXT NOT NULL,
        current_step TEXT,
        requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        started_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ,
        error_message TEXT,
        state_payload JSONB NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    "CREATE INDEX IF NOT EXISTS agent_runs_market_status_idx ON agent_runs (market_id, status, requested_at DESC)",
    "CREATE INDEX IF NOT EXISTS agent_runs_entity_idx ON agent_runs (entity_type, entity_id, workflow_type, requested_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS agent_run_events (
        id BIGSERIAL PRIMARY KEY,
        agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        step_name TEXT NOT NULL,
        status TEXT NOT NULL,
        message TEXT NOT NULL,
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS agent_run_events_run_idx ON agent_run_events (agent_run_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS agent_tasks (
        id TEXT PRIMARY KEY,
        agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        parent_task_id TEXT REFERENCES agent_tasks(id) ON DELETE SET NULL,
        task_type TEXT NOT NULL,
        status TEXT NOT NULL,
        attempt INTEGER NOT NULL DEFAULT 1,
        input_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        output_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        error_message TEXT,
        started_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS agent_tasks_run_idx ON agent_tasks (agent_run_id, created_at)",
    "CREATE INDEX IF NOT EXISTS agent_tasks_parent_idx ON agent_tasks (parent_task_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS agent_artifacts (
        id TEXT PRIMARY KEY,
        agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        produced_by_task_id TEXT REFERENCES agent_tasks(id) ON DELETE SET NULL,
        artifact_type TEXT NOT NULL,
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        source_document_id TEXT REFERENCES source_documents(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS agent_artifacts_run_idx ON agent_artifacts (agent_run_id, created_at)",
    "CREATE INDEX IF NOT EXISTS agent_artifacts_task_idx ON agent_artifacts (produced_by_task_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS agent_proposals (
        id TEXT PRIMARY KEY,
        agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        produced_by_task_id TEXT REFERENCES agent_tasks(id) ON DELETE SET NULL,
        proposal_type TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'proposed',
        review_state TEXT NOT NULL DEFAULT 'not_required',
        decision TEXT,
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
        promoted_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS agent_proposals_run_idx ON agent_proposals (agent_run_id, created_at)",
    "CREATE INDEX IF NOT EXISTS agent_proposals_review_idx ON agent_proposals (review_state, created_at)",
    """
    CREATE TABLE IF NOT EXISTS agent_validations (
        id TEXT PRIMARY KEY,
        agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        proposal_id TEXT NOT NULL REFERENCES agent_proposals(id) ON DELETE CASCADE,
        validator_name TEXT NOT NULL,
        outcome TEXT NOT NULL,
        message TEXT NOT NULL,
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS agent_validations_run_idx ON agent_validations (agent_run_id, created_at)",
    "CREATE INDEX IF NOT EXISTS agent_validations_proposal_idx ON agent_validations (proposal_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS agent_reviews (
        id TEXT PRIMARY KEY,
        agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        proposal_id TEXT NOT NULL REFERENCES agent_proposals(id) ON DELETE CASCADE,
        status TEXT NOT NULL,
        reviewer TEXT,
        notes TEXT,
        decision_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS agent_reviews_proposal_unique_idx ON agent_reviews (proposal_id)",
    "CREATE INDEX IF NOT EXISTS agent_reviews_run_status_idx ON agent_reviews (agent_run_id, status, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS buildings_market_idx ON buildings (market_id)",
    "CREATE INDEX IF NOT EXISTS addresses_market_idx ON addresses (market_id)",
    """
    ALTER TABLE parcels
    ADD COLUMN IF NOT EXISTS ownership_enrichment_status TEXT NOT NULL DEFAULT 'not_started'
    """,
    "ALTER TABLE parcels ADD COLUMN IF NOT EXISTS ownership_last_enriched_at TIMESTAMPTZ",
    "ALTER TABLE parcels ADD COLUMN IF NOT EXISTS ownership_note TEXT",
    """
    ALTER TABLE parcels
    ADD COLUMN IF NOT EXISTS ownership_metadata JSONB NOT NULL DEFAULT '{}'::jsonb
    """,
    """
    CREATE TABLE IF NOT EXISTS organizations (
        id TEXT PRIMARY KEY,
        market_id TEXT NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        normalized_name TEXT NOT NULL,
        nj_entity_id TEXT,
        matched_search_name TEXT,
        domicile_city TEXT,
        entity_type TEXT,
        entity_type_title TEXT,
        status TEXT,
        formation_date DATE,
        registered_agent TEXT,
        source_document_id TEXT REFERENCES source_documents(id),
        last_enriched_at TIMESTAMPTZ,
        raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS organizations_nj_entity_unique_idx
        ON organizations (nj_entity_id)
        WHERE nj_entity_id IS NOT NULL
    """,
    "CREATE INDEX IF NOT EXISTS organizations_market_name_idx ON organizations (market_id, normalized_name)",
    """
    CREATE TABLE IF NOT EXISTS people (
        id TEXT PRIMARY KEY,
        full_name TEXT NOT NULL,
        normalized_name TEXT NOT NULL,
        source_document_id TEXT REFERENCES source_documents(id),
        last_enriched_at TIMESTAMPTZ,
        raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    "CREATE INDEX IF NOT EXISTS people_normalized_name_idx ON people (normalized_name)",
    """
    CREATE TABLE IF NOT EXISTS recorder_documents (
        id TEXT PRIMARY KEY,
        parcel_id TEXT NOT NULL REFERENCES parcels(id) ON DELETE CASCADE,
        document_number TEXT NOT NULL,
        document_type TEXT NOT NULL,
        recorded_at DATE NOT NULL,
        municipality TEXT,
        block TEXT,
        lot TEXT,
        book TEXT,
        page TEXT,
        document_category TEXT NOT NULL DEFAULT 'other',
        grantors JSONB NOT NULL DEFAULT '[]'::jsonb,
        grantees JSONB NOT NULL DEFAULT '[]'::jsonb,
        consideration NUMERIC(14, 2),
        source_document_id TEXT REFERENCES source_documents(id),
        last_enriched_at TIMESTAMPTZ,
        raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    "ALTER TABLE recorder_documents ADD COLUMN IF NOT EXISTS document_category TEXT NOT NULL DEFAULT 'other'",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS recorder_documents_unique_idx
        ON recorder_documents (parcel_id, document_number, recorded_at)
    """,
    "CREATE INDEX IF NOT EXISTS recorder_documents_parcel_idx ON recorder_documents (parcel_id, recorded_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS parcel_ownership_claims (
        id TEXT PRIMARY KEY,
        parcel_id TEXT NOT NULL REFERENCES parcels(id) ON DELETE CASCADE,
        organization_id TEXT REFERENCES organizations(id) ON DELETE SET NULL,
        recorder_document_id TEXT NOT NULL REFERENCES recorder_documents(id) ON DELETE CASCADE,
        owner_name TEXT NOT NULL,
        confidence_tier TEXT NOT NULL,
        valid_from DATE,
        valid_to DATE,
        superseded_by TEXT REFERENCES parcel_ownership_claims(id),
        source_document_id TEXT REFERENCES source_documents(id),
        last_enriched_at TIMESTAMPTZ,
        notes TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS parcel_ownership_claims_unique_idx
        ON parcel_ownership_claims (parcel_id, recorder_document_id, owner_name)
    """,
    """
    CREATE INDEX IF NOT EXISTS parcel_ownership_claims_current_idx
        ON parcel_ownership_claims (parcel_id, valid_to, valid_from DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS parcel_financing_claims (
        id TEXT PRIMARY KEY,
        parcel_id TEXT NOT NULL REFERENCES parcels(id) ON DELETE CASCADE,
        organization_id TEXT REFERENCES organizations(id) ON DELETE SET NULL,
        lender_name TEXT NOT NULL,
        recorder_document_id TEXT NOT NULL REFERENCES recorder_documents(id) ON DELETE CASCADE,
        claim_type TEXT NOT NULL,
        amount NUMERIC(14, 2),
        confidence_tier TEXT NOT NULL,
        valid_from DATE,
        valid_to DATE,
        superseded_by TEXT REFERENCES parcel_financing_claims(id),
        source_document_id TEXT REFERENCES source_documents(id),
        last_enriched_at TIMESTAMPTZ,
        notes TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS parcel_financing_claims_unique_idx
        ON parcel_financing_claims (parcel_id, recorder_document_id, lender_name, claim_type)
    """,
    """
    CREATE INDEX IF NOT EXISTS parcel_financing_claims_parcel_idx
        ON parcel_financing_claims (parcel_id, valid_to, valid_from DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS organization_roles (
        id TEXT PRIMARY KEY,
        organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        person_id TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        role_title TEXT NOT NULL,
        role_type TEXT NOT NULL DEFAULT 'officer',
        source_document_id TEXT REFERENCES source_documents(id),
        last_enriched_at TIMESTAMPTZ,
        raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS organization_roles_unique_idx
        ON organization_roles (organization_id, person_id, role_title)
    """,
    "CREATE INDEX IF NOT EXISTS organization_roles_org_idx ON organization_roles (organization_id)",
    """
    CREATE TABLE IF NOT EXISTS person_affiliations (
        id TEXT PRIMARY KEY,
        market_id TEXT NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
        person_id TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        organization_id TEXT REFERENCES organizations(id) ON DELETE SET NULL,
        title TEXT NOT NULL,
        affiliation_type TEXT NOT NULL,
        persona TEXT,
        confidence_tier TEXT NOT NULL DEFAULT 'probable',
        valid_from DATE,
        valid_to DATE,
        source_document_id TEXT REFERENCES source_documents(id),
        last_verified_at TIMESTAMPTZ,
        raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS person_affiliations_unique_idx
        ON person_affiliations (person_id, organization_id, title, affiliation_type)
    """,
    "CREATE INDEX IF NOT EXISTS person_affiliations_market_idx ON person_affiliations (market_id, persona)",
    """
    CREATE TABLE IF NOT EXISTS contact_points (
        id TEXT PRIMARY KEY,
        market_id TEXT NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
        person_id TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        organization_id TEXT REFERENCES organizations(id) ON DELETE SET NULL,
        contact_type TEXT NOT NULL,
        contact_value TEXT NOT NULL,
        label TEXT,
        source_family TEXT,
        source_document_id TEXT REFERENCES source_documents(id),
        confidence_tier TEXT NOT NULL DEFAULT 'probable',
        is_primary BOOLEAN NOT NULL DEFAULT FALSE,
        last_verified_at TIMESTAMPTZ,
        raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS contact_points_unique_idx
        ON contact_points (person_id, contact_type, contact_value)
    """,
    "CREATE INDEX IF NOT EXISTS contact_points_market_idx ON contact_points (market_id, contact_type, is_primary)",
    """
    CREATE TABLE IF NOT EXISTS lead_candidates (
        id TEXT PRIMARY KEY,
        market_id TEXT NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
        person_id TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        organization_id TEXT REFERENCES organizations(id) ON DELETE SET NULL,
        persona TEXT NOT NULL,
        lead_status TEXT NOT NULL DEFAULT 'candidate',
        lead_score NUMERIC(5, 2) NOT NULL DEFAULT 0,
        role_relevance_score NUMERIC(5, 2) NOT NULL DEFAULT 0,
        market_relevance_score NUMERIC(5, 2) NOT NULL DEFAULT 0,
        contactability_score NUMERIC(5, 2) NOT NULL DEFAULT 0,
        evidence_score NUMERIC(5, 2) NOT NULL DEFAULT 0,
        review_state TEXT NOT NULL DEFAULT 'not_required',
        why_person_matters TEXT,
        agent_run_id TEXT REFERENCES agent_runs(id) ON DELETE SET NULL,
        source_document_id TEXT REFERENCES source_documents(id),
        last_verified_at TIMESTAMPTZ,
        raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS lead_candidates_unique_idx
        ON lead_candidates (market_id, person_id, persona)
    """,
    "CREATE INDEX IF NOT EXISTS lead_candidates_market_idx ON lead_candidates (market_id, persona, lead_score DESC)",
    "CREATE INDEX IF NOT EXISTS lead_candidates_review_idx ON lead_candidates (review_state, updated_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS lead_evidence_links (
        id TEXT PRIMARY KEY,
        lead_id TEXT NOT NULL REFERENCES lead_candidates(id) ON DELETE CASCADE,
        evidence_type TEXT NOT NULL,
        parcel_id TEXT REFERENCES parcels(id) ON DELETE SET NULL,
        organization_id TEXT REFERENCES organizations(id) ON DELETE SET NULL,
        person_id TEXT REFERENCES people(id) ON DELETE SET NULL,
        source_document_id TEXT REFERENCES source_documents(id),
        notes TEXT,
        raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS lead_evidence_links_lead_idx ON lead_evidence_links (lead_id, evidence_type)",
    "CREATE INDEX IF NOT EXISTS lead_evidence_links_parcel_idx ON lead_evidence_links (parcel_id)",
]


def get_connection(database_url):
    return psycopg.connect(database_url, row_factory=dict_row)


def _lock_key(value):
    return zlib.crc32(value.encode("utf-8")) & 0x7FFFFFFF


@contextmanager
def advisory_job_lock(connection, namespace, name, wait_timeout_seconds=0):
    key = _lock_key(name)
    acquired = False
    deadline = time.monotonic() + max(wait_timeout_seconds, 0)
    while not acquired:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s, %s) AS acquired", (namespace, key))
            acquired = bool(cursor.fetchone()["acquired"])
        if acquired:
            break
        if wait_timeout_seconds <= 0 or time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    if not acquired:
        raise RuntimeError(
            "Another job for %s is already running. Wait for it to finish or terminate the stale process first."
            % name
        )

    try:
        yield
    except Exception:
        connection.rollback()
        raise
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s, %s)", (namespace, key))
        connection.commit()


def ensure_schema(connection):
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with advisory_job_lock(connection, 9301, "schema", wait_timeout_seconds=30):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    to_regclass('public.markets') AS markets_table,
                    to_regclass('public.parcel_financing_claims') AS parcel_financing_claims_table,
                    to_regclass('public.agent_reviews') AS agent_reviews_table,
                    to_regclass('public.lead_candidates') AS lead_candidates_table
                """
            )
            row = cursor.fetchone()
            has_schema = bool(row["markets_table"])
            has_runtime_tables = bool(
                row["parcel_financing_claims_table"]
                and row["agent_reviews_table"]
                and row["lead_candidates_table"]
            )
        if not has_schema:
            schema_sql = SCHEMA_PATH.read_text()
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '5s'")
                cursor.execute(schema_sql)
        elif not has_runtime_tables:
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '5s'")
                for statement in RUNTIME_MIGRATIONS:
                    cursor.execute(statement)
    connection.commit()
    _SCHEMA_READY = True
