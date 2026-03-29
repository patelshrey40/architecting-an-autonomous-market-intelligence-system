CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TYPE relationship_type AS ENUM (
    'PARCEL_CONTAINS_BUILDING',
    'BUILDING_HAS_ADDRESS',
    'ORGANIZATION_OWNS_PARCEL',
    'PERSON_IS_OFFICER_OF_ORGANIZATION',
    'FUND_FINANCED_PARCEL',
    'ORGANIZATION_DEVELOPED_PROJECT',
    'PERSON_IS_PRINCIPAL_OF_PROJECT',
    'PERSON_SPOKE_AT_EVENT',
    'ORGANIZATION_SPONSORED_EVENT',
    'PERSON_SERVES_ON_ORGANIZATION'
);

CREATE TYPE confidence_tier AS ENUM (
    'verified',
    'probable',
    'inferred',
    'conflict',
    'partial'
);

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

CREATE TABLE organizations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    normalized_name TEXT GENERATED ALWAYS AS (
        regexp_replace(lower(name), '[^a-z0-9]+', '', 'g')
    ) STORED,
    category TEXT NOT NULL,
    status TEXT NOT NULL,
    formation_state TEXT,
    registered_agent TEXT,
    formation_date DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX organizations_name_trgm_idx ON organizations USING gin (name gin_trgm_ops);

CREATE TABLE people (
    id TEXT PRIMARY KEY,
    full_name TEXT NOT NULL,
    normalized_name TEXT GENERATED ALWAYS AS (
        regexp_replace(lower(full_name), '[^a-z0-9]+', '', 'g')
    ) STORED,
    primary_title TEXT,
    email TEXT,
    phone TEXT,
    city TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX people_name_trgm_idx ON people USING gin (full_name gin_trgm_ops);

CREATE TABLE parcels (
    id TEXT PRIMARY KEY,
    parcel_pin TEXT NOT NULL UNIQUE,
    county TEXT NOT NULL,
    municipality TEXT NOT NULL,
    block TEXT NOT NULL,
    lot TEXT NOT NULL,
    property_class_code TEXT NOT NULL,
    zoning_code TEXT,
    land_value NUMERIC(14, 2) NOT NULL DEFAULT 0,
    improvement_value NUMERIC(14, 2) NOT NULL DEFAULT 0,
    total_assessed_value NUMERIC(14, 2) GENERATED ALWAYS AS (land_value + improvement_value) STORED,
    classification property_classification NOT NULL DEFAULT 'UNKNOWN',
    priority_score NUMERIC(5, 1),
    priority_tier priority_tier,
    geom geometry(MultiPolygon, 4326),
    centroid geometry(Point, 4326),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX parcels_geom_idx ON parcels USING gist (geom);
CREATE INDEX parcels_centroid_idx ON parcels USING gist (centroid);
CREATE INDEX parcels_block_lot_idx ON parcels (county, municipality, block, lot);
CREATE INDEX parcels_classification_idx ON parcels (classification, priority_tier);

CREATE TABLE buildings (
    id TEXT PRIMARY KEY,
    parcel_id TEXT NOT NULL REFERENCES parcels(id) ON DELETE CASCADE,
    overture_gers_id TEXT,
    building_name TEXT,
    square_feet INTEGER,
    year_built INTEGER,
    geom geometry(MultiPolygon, 4326),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX buildings_geom_idx ON buildings USING gist (geom);

CREATE TABLE addresses (
    id TEXT PRIMARY KEY,
    building_id TEXT REFERENCES buildings(id) ON DELETE SET NULL,
    parcel_id TEXT REFERENCES parcels(id) ON DELETE SET NULL,
    normalized_address TEXT NOT NULL,
    geom geometry(Point, 4326),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX addresses_geom_idx ON addresses USING gist (geom);

CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    parcel_id TEXT REFERENCES parcels(id) ON DELETE SET NULL,
    description TEXT,
    key_date DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE events (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    event_date DATE NOT NULL,
    city TEXT NOT NULL,
    organizer TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE claims (
    id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    predicate relationship_type NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    confidence confidence_tier NOT NULL,
    source_document_id TEXT NOT NULL REFERENCES source_documents(id),
    observed_at TIMESTAMPTZ NOT NULL,
    valid_from TIMESTAMPTZ,
    valid_to TIMESTAMPTZ,
    superseded_by TEXT REFERENCES claims(id),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_from < valid_to)
);

CREATE INDEX claims_subject_idx ON claims (subject_type, subject_id);
CREATE INDEX claims_object_idx ON claims (object_type, object_id);
CREATE INDEX claims_confidence_idx ON claims (confidence);

CREATE TABLE audit_log (
    id BIGSERIAL PRIMARY KEY,
    table_name TEXT NOT NULL,
    record_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    source_document_id TEXT REFERENCES source_documents(id),
    payload JSONB NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
