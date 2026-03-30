CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TYPE property_classification AS ENUM (
    'COMMERCIAL',
    'INDUSTRIAL',
    'MULTIFAMILY',
    'RESIDENTIAL',
    'VACANT',
    'FARM',
    'EXEMPT_PUBLIC',
    'EXEMPT_RELIGIOUS_CHARITABLE',
    'EXEMPT_CEMETERY',
    'EXEMPT_OTHER',
    'RAILROAD',
    'UNKNOWN'
);

CREATE TYPE priority_tier AS ENUM (
    'Tier 1',
    'Tier 2',
    'Tier 3',
    'Tier 4'
);

CREATE TABLE source_documents (
    id TEXT PRIMARY KEY,
    source_name TEXT NOT NULL,
    title TEXT NOT NULL,
    source_url TEXT,
    access_date DATE NOT NULL,
    document_type TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE ingest_runs (
    id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    qa_report JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE markets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    state TEXT NOT NULL,
    source_document_id TEXT REFERENCES source_documents(id),
    last_ingested_at TIMESTAMPTZ,
    geom geometry(MultiPolygon, 4326) NOT NULL,
    centroid geometry(Point, 4326) GENERATED ALWAYS AS (ST_PointOnSurface(geom)) STORED
);

CREATE INDEX markets_geom_idx ON markets USING gist (geom);

CREATE TABLE agent_runs (
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
);

CREATE INDEX agent_runs_market_status_idx ON agent_runs (market_id, status, requested_at DESC);
CREATE INDEX agent_runs_entity_idx ON agent_runs (entity_type, entity_id, workflow_type, requested_at DESC);

CREATE TABLE agent_run_events (
    id BIGSERIAL PRIMARY KEY,
    agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    step_name TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX agent_run_events_run_idx ON agent_run_events (agent_run_id, created_at);

CREATE TABLE agent_tasks (
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
);

CREATE INDEX agent_tasks_run_idx ON agent_tasks (agent_run_id, created_at);
CREATE INDEX agent_tasks_parent_idx ON agent_tasks (parent_task_id, created_at);

CREATE TABLE agent_artifacts (
    id TEXT PRIMARY KEY,
    agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    produced_by_task_id TEXT REFERENCES agent_tasks(id) ON DELETE SET NULL,
    artifact_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_document_id TEXT REFERENCES source_documents(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX agent_artifacts_run_idx ON agent_artifacts (agent_run_id, created_at);
CREATE INDEX agent_artifacts_task_idx ON agent_artifacts (produced_by_task_id, created_at);

CREATE TABLE agent_proposals (
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
);

CREATE INDEX agent_proposals_run_idx ON agent_proposals (agent_run_id, created_at);
CREATE INDEX agent_proposals_review_idx ON agent_proposals (review_state, created_at);

CREATE TABLE agent_validations (
    id TEXT PRIMARY KEY,
    agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    proposal_id TEXT NOT NULL REFERENCES agent_proposals(id) ON DELETE CASCADE,
    validator_name TEXT NOT NULL,
    outcome TEXT NOT NULL,
    message TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX agent_validations_run_idx ON agent_validations (agent_run_id, created_at);
CREATE INDEX agent_validations_proposal_idx ON agent_validations (proposal_id, created_at);

CREATE TABLE agent_reviews (
    id TEXT PRIMARY KEY,
    agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    proposal_id TEXT NOT NULL REFERENCES agent_proposals(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    reviewer TEXT,
    notes TEXT,
    decision_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX agent_reviews_proposal_unique_idx ON agent_reviews (proposal_id);
CREATE INDEX agent_reviews_run_status_idx ON agent_reviews (agent_run_id, status, updated_at DESC);

CREATE TABLE parcels (
    id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
    parcel_pin TEXT NOT NULL UNIQUE,
    county TEXT NOT NULL,
    municipality TEXT NOT NULL,
    block TEXT NOT NULL,
    lot TEXT NOT NULL,
    qualifier TEXT,
    property_location TEXT,
    owner_name TEXT,
    property_class_code TEXT NOT NULL,
    property_use_code TEXT,
    land_description TEXT,
    zoning_code TEXT,
    land_value NUMERIC(14, 2) NOT NULL DEFAULT 0,
    improvement_value NUMERIC(14, 2) NOT NULL DEFAULT 0,
    total_assessed_value NUMERIC(14, 2) GENERATED ALWAYS AS (land_value + improvement_value) STORED,
    sale_price NUMERIC(14, 2),
    sale_date DATE,
    year_built INTEGER,
    calculated_acres NUMERIC(14, 4),
    building_count INTEGER NOT NULL DEFAULT 0,
    place_count INTEGER NOT NULL DEFAULT 0,
    building_sqft_total NUMERIC(14, 2) NOT NULL DEFAULT 0,
    assessed_value_percentile NUMERIC(5, 2),
    class_relevance_score NUMERIC(5, 2),
    building_sqft_percentile NUMERIC(5, 2),
    place_density_signal NUMERIC(5, 2),
    priority_score NUMERIC(5, 2),
    priority_tier priority_tier,
    classification property_classification NOT NULL DEFAULT 'UNKNOWN',
    ownership_enrichment_status TEXT NOT NULL DEFAULT 'not_started',
    ownership_last_enriched_at TIMESTAMPTZ,
    ownership_note TEXT,
    ownership_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_document_id TEXT REFERENCES source_documents(id),
    last_ingested_at TIMESTAMPTZ,
    geom geometry(MultiPolygon, 4326) NOT NULL,
    centroid geometry(Point, 4326) GENERATED ALWAYS AS (ST_PointOnSurface(geom)) STORED
);

CREATE INDEX parcels_geom_idx ON parcels USING gist (geom);
CREATE INDEX parcels_centroid_idx ON parcels USING gist (centroid);
CREATE INDEX parcels_market_idx ON parcels (market_id, classification, priority_tier);
CREATE INDEX parcels_block_lot_idx ON parcels (county, municipality, block, lot);
CREATE INDEX parcels_location_trgm_idx ON parcels USING gin (property_location gin_trgm_ops);

CREATE TABLE buildings (
    id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
    parcel_id TEXT REFERENCES parcels(id) ON DELETE SET NULL,
    external_id TEXT UNIQUE,
    building_name TEXT,
    building_sqft NUMERIC(14, 2),
    height_m NUMERIC(10, 2),
    place_count INTEGER NOT NULL DEFAULT 0,
    source_document_id TEXT REFERENCES source_documents(id),
    last_ingested_at TIMESTAMPTZ,
    geom geometry(MultiPolygon, 4326) NOT NULL,
    centroid geometry(Point, 4326) GENERATED ALWAYS AS (ST_PointOnSurface(geom)) STORED
);

CREATE INDEX buildings_geom_idx ON buildings USING gist (geom);
CREATE INDEX buildings_market_idx ON buildings (market_id);
CREATE INDEX buildings_centroid_idx ON buildings USING gist (centroid);
CREATE INDEX buildings_parcel_idx ON buildings (parcel_id);

CREATE TABLE addresses (
    id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
    building_id TEXT REFERENCES buildings(id) ON DELETE SET NULL,
    parcel_id TEXT REFERENCES parcels(id) ON DELETE SET NULL,
    external_id TEXT UNIQUE,
    display_name TEXT NOT NULL,
    source_document_id TEXT REFERENCES source_documents(id),
    last_ingested_at TIMESTAMPTZ,
    geom geometry(Point, 4326) NOT NULL
);

CREATE INDEX addresses_geom_idx ON addresses USING gist (geom);
CREATE INDEX addresses_market_idx ON addresses (market_id);
CREATE INDEX addresses_parcel_idx ON addresses (parcel_id);
CREATE INDEX addresses_display_trgm_idx ON addresses USING gin (display_name gin_trgm_ops);

CREATE TABLE redevelopment_areas (
    id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    short_name TEXT,
    plan_link TEXT,
    source_document_id TEXT REFERENCES source_documents(id),
    last_ingested_at TIMESTAMPTZ,
    geom geometry(MultiPolygon, 4326) NOT NULL
);

CREATE INDEX redevelopment_areas_geom_idx ON redevelopment_areas USING gist (geom);
CREATE INDEX redevelopment_areas_market_idx ON redevelopment_areas (market_id);

CREATE TABLE transit_stops (
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
);

CREATE INDEX transit_stops_geom_idx ON transit_stops USING gist (geom);
CREATE INDEX transit_stops_market_idx ON transit_stops (market_id, stop_type);

CREATE TABLE organizations (
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
);

CREATE UNIQUE INDEX organizations_nj_entity_unique_idx
    ON organizations (nj_entity_id)
    WHERE nj_entity_id IS NOT NULL;
CREATE INDEX organizations_market_name_idx ON organizations (market_id, normalized_name);

CREATE TABLE people (
    id TEXT PRIMARY KEY,
    full_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    source_document_id TEXT REFERENCES source_documents(id),
    last_enriched_at TIMESTAMPTZ,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX people_normalized_name_idx ON people (normalized_name);

CREATE TABLE recorder_documents (
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
);

CREATE UNIQUE INDEX recorder_documents_unique_idx
    ON recorder_documents (parcel_id, document_number, recorded_at);
CREATE INDEX recorder_documents_parcel_idx ON recorder_documents (parcel_id, recorded_at DESC);

CREATE TABLE parcel_ownership_claims (
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
);

CREATE UNIQUE INDEX parcel_ownership_claims_unique_idx
    ON parcel_ownership_claims (parcel_id, recorder_document_id, owner_name);
CREATE INDEX parcel_ownership_claims_current_idx
    ON parcel_ownership_claims (parcel_id, valid_to, valid_from DESC);

CREATE TABLE parcel_financing_claims (
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
);

CREATE UNIQUE INDEX parcel_financing_claims_unique_idx
    ON parcel_financing_claims (parcel_id, recorder_document_id, lender_name, claim_type);
CREATE INDEX parcel_financing_claims_parcel_idx
    ON parcel_financing_claims (parcel_id, valid_to, valid_from DESC);

CREATE TABLE organization_roles (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    person_id TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    role_title TEXT NOT NULL,
    role_type TEXT NOT NULL DEFAULT 'officer',
    source_document_id TEXT REFERENCES source_documents(id),
    last_enriched_at TIMESTAMPTZ,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE UNIQUE INDEX organization_roles_unique_idx
    ON organization_roles (organization_id, person_id, role_title);
CREATE INDEX organization_roles_org_idx ON organization_roles (organization_id);

CREATE TABLE person_affiliations (
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
);

CREATE UNIQUE INDEX person_affiliations_unique_idx
    ON person_affiliations (person_id, organization_id, title, affiliation_type);
CREATE INDEX person_affiliations_market_idx ON person_affiliations (market_id, persona);

CREATE TABLE contact_points (
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
);

CREATE UNIQUE INDEX contact_points_unique_idx
    ON contact_points (person_id, contact_type, contact_value);
CREATE INDEX contact_points_market_idx ON contact_points (market_id, contact_type, is_primary);

CREATE TABLE lead_candidates (
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
);

CREATE UNIQUE INDEX lead_candidates_unique_idx
    ON lead_candidates (market_id, person_id, persona);
CREATE INDEX lead_candidates_market_idx ON lead_candidates (market_id, persona, lead_score DESC);
CREATE INDEX lead_candidates_review_idx ON lead_candidates (review_state, updated_at DESC);

CREATE TABLE lead_evidence_links (
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
);

CREATE INDEX lead_evidence_links_lead_idx ON lead_evidence_links (lead_id, evidence_type);
CREATE INDEX lead_evidence_links_parcel_idx ON lead_evidence_links (parcel_id);

CREATE TABLE audit_log (
    id BIGSERIAL PRIMARY KEY,
    table_name TEXT NOT NULL,
    record_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    source_document_id TEXT REFERENCES source_documents(id),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
