<img width="1460" height="807" alt="Screenshot 2026-03-30 at 2 11 23 AM" src="https://github.com/user-attachments/assets/6e431600-1bd4-4675-990b-b6dd99cfcceb" />

# Architecting an Autonomous Market Intelligence System

This repository now implements the Newark real-data foundation slice plus a narrow ownership demo from the larger roadmap.

The app no longer runs on seeded parcel, people, or summit-target data. It focuses on the first real-data layer:

- official Newark parcel and MOD-IV data from NJGIN
- official Newark place boundary validation from Census TIGER
- Overture buildings, addresses, and places
- Essex County recorder owner-of-record enrichment
- NJ business search matching for owner entity summaries
- PostGIS spatial joins and deterministic scoring
- a Leaflet + OpenStreetMap map UI backed by FastAPI APIs

## What this phase ships

- repeatable ingest command: `python -m app.cli ingest-newark`
- repeatable ownership enrichment command: `python -m app.cli enrich-newark-ownership --top-n 50`
- Docker Compose stack with Postgres 16 + PostGIS and the FastAPI app
- database-backed Newark APIs
- real-data-only Newark UI with:
  - summary KPI cards
  - interactive parcel map
  - parcel table for the current viewport
  - parcel detail drawer with score breakdown, ownership, and source provenance

## Data sources

- [NJGIN Parcels and MOD-IV Composite](https://nj.gov/njgin/edata/parcels/index.html)
- [Census TIGER/Line geodatabases](https://www.census.gov/geographies/mapping-files/time-series/geo/tiger-geodatabase-file.2025.html)
- [Overture Maps Python Client](https://docs.overturemaps.org/getting-data/overturemaps-py/)
- [OpenStreetMap tile policy](https://operations.osmfoundation.org/policies/tiles/)

## Current scope

This phase is intentionally limited to Newark foundation data.

Included:

- parcels
- buildings
- addresses
- place density
- classification
- deterministic scoring
- owner-of-record deed summaries
- NJ entity summary matching
- QA checks
- provenance records

Not included yet:

- entity resolution
- people/contact enrichment
- summit targeting
- public deployment

## Run locally

### 1. Start PostGIS

```bash
docker compose up -d db
```

### 2. Install Python dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Ingest Newark data

```bash
python -m app.cli ingest-newark
```

The command caches source downloads under `.data/raw` and reuses them on later runs.

### 4. Start the app

```bash
uvicorn app.main:app --reload
```

Then open `http://127.0.0.1:8000`.

### 5. Enrich top Newark ownership targets

```bash
python -m app.cli enrich-newark-ownership --top-n 50
```

This stores owner-of-record deed summaries for the top Newark Tier 1 parcels and lets the UI open the ownership drawer instantly for those parcels. The ownership endpoint will also enrich a selected parcel on demand if it has not been enriched yet.

## Docker-first run

```bash
docker compose up -d db
docker compose run --rm app python -m app.cli ingest-newark
docker compose up app
```

## API endpoints

- `GET /api/newark/summary`
- `GET /api/newark/parcels?bbox=minLon,minLat,maxLon,maxLat&classification=&tier=&q=&min_value=`
- `GET /api/newark/buildings?bbox=minLon,minLat,maxLon,maxLat`
- `GET /api/newark/parcels/{parcel_id}`
- `GET /api/newark/parcels/{parcel_id}/ownership`
- `GET /healthz`

## Tests

```bash
python -m unittest discover -s tests -v
```

What is covered:

- NJ class-code mapping and score formulas
- API bbox parsing and GeoJSON serialization
- fixture-based Newark ingest parsing
- Essex recorder and NJ business search parser coverage
- ownership enrichment fixture and endpoint coverage
- PostGIS integration test using tiny checked-in fixtures when a database is available

## Notes on the map

- Parcel polygons are fetched for the current map viewport only.
- Building footprints load automatically at higher zoom.
- OpenStreetMap attribution is always displayed.
- This is a low-volume local demo implementation. If the app becomes public, the tile strategy should be revisited.
