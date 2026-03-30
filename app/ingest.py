from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
import zipfile

import requests
import shapefile
from psycopg.types.json import Json

from app.config import get_settings
from app.db import LOCK_NAMESPACE_INGEST, advisory_job_lock, ensure_schema, get_connection


REQUEST_TIMEOUT = 120
PARSER_VERSION = "newark-foundation-v1"
FIXTURE_RESET_GUARD_PARCEL_LIMIT = 100


def _utc_now():
    return datetime.now(timezone.utc)


def _run_id():
    return "ingest-%s" % _utc_now().strftime("%Y%m%d%H%M%S")


def _read_geojson(path):
    with path.open() as handle:
        return json.load(handle)


def _write_geojson(path, payload):
    path.write_text(json.dumps(payload))


def _log_step(progress, message):
    if progress:
        progress(message)


def _download_file(url, destination):
    response = requests.get(url, timeout=REQUEST_TIMEOUT, stream=True)
    response.raise_for_status()
    with destination.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                handle.write(chunk)


def _chunked(values, size):
    for index in range(0, len(values), size):
        yield values[index:index + size]


def _slugify(text):
    return "-".join(text.lower().replace("&", "and").replace("/", " ").split())


def _maybe_float(value):
    if value in (None, "", "null"):
        return None
    return float(value)


def _maybe_int(value):
    if value in (None, "", "null"):
        return None
    return int(value)


def _parse_sale_date(raw_value):
    value = (raw_value or "").strip()
    if not value:
        return None
    if len(value) == 6 and value.isdigit():
        month = int(value[:2])
        day = int(value[2:4])
        year = int(value[4:])
        year = 2000 + year if year < 50 else 1900 + year
        try:
            return datetime(year, month, day).date().isoformat()
        except ValueError:
            return None
    return None


def _extract_feature_name(properties):
    names = properties.get("names") or {}
    if isinstance(names, dict):
        for candidate_key in ("primary", "common", "default", "name"):
            candidate_value = names.get(candidate_key)
            if isinstance(candidate_value, str) and candidate_value.strip():
                return candidate_value.strip()
    for key in ("name", "display_name", "label"):
        candidate_value = properties.get(key)
        if isinstance(candidate_value, str) and candidate_value.strip():
            return candidate_value.strip()
    return None


def _extract_address_display_name(properties):
    for key in (
        "freeform",
        "display_name",
        "label",
        "address",
        "formatted_address",
    ):
        value = properties.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    lines = []
    for key in ("housenumber", "number", "street", "locality", "postal_city", "region", "postcode"):
        value = properties.get(key)
        if isinstance(value, str) and value.strip():
            lines.append(value.strip())
    if lines:
        return ", ".join(lines)

    for candidate in properties.values():
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return "Unknown address"


def _boundary_from_geojson_feature(feature):
    return {"type": "Feature", "geometry": feature["geometry"], "properties": feature.get("properties", {})}


def load_newark_boundary(raw_dir, tmp_dir, fixtures_dir=None, tiger_place_zip_url=None):
    if fixtures_dir:
        fixture_path = Path(fixtures_dir) / "boundary.geojson"
        return _boundary_from_geojson_feature(_read_geojson(fixture_path))

    cache_path = raw_dir / "newark-boundary.geojson"
    if cache_path.exists():
        return _boundary_from_geojson_feature(_read_geojson(cache_path))

    zip_path = raw_dir / "tl_2025_34_place.zip"
    if not zip_path.exists():
        _download_file(tiger_place_zip_url, zip_path)

    extract_dir = tmp_dir / "tiger_place"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(extract_dir)

    shp_path = next(extract_dir.glob("*.shp"))
    reader = shapefile.Reader(str(shp_path))
    field_names = [field[0] for field in reader.fields[1:]]
    selected = None
    for shape_record in reader.iterShapeRecords():
        record = dict(zip(field_names, shape_record.record))
        if record.get("NAME", "").upper() == "NEWARK":
            selected = {
                "type": "Feature",
                "geometry": shape_record.shape.__geo_interface__,
                "properties": record,
            }
            break

    if selected is None:
        raise RuntimeError("Could not find Newark boundary in TIGER place file")

    _write_geojson(cache_path, selected)
    return selected


def load_newark_parcels(raw_dir, fixtures_dir=None, parcels_url=None):
    if fixtures_dir:
        return _read_geojson(Path(fixtures_dir) / "parcels.geojson")

    cache_path = raw_dir / "newark-parcels.geojson"
    if cache_path.exists():
        cached_payload = _read_geojson(cache_path)
        if cached_payload.get("features"):
            return cached_payload

    id_response = requests.get(
        parcels_url,
        params={
            "f": "json",
            "where": "COUNTY = 'ESSEX' AND MUN_NAME = 'NEWARK CITY'",
            "returnIdsOnly": "true",
        },
        timeout=REQUEST_TIMEOUT,
    )
    id_response.raise_for_status()
    object_ids = id_response.json().get("objectIds") or []
    features = []

    for object_id_batch in _chunked(object_ids, 200):
        query_response = requests.get(
            parcels_url,
            params={
                "f": "geojson",
                "objectIds": ",".join(str(object_id) for object_id in object_id_batch),
                "outFields": "*",
                "outSR": 4326,
            },
            timeout=REQUEST_TIMEOUT,
        )
        query_response.raise_for_status()
        features.extend(query_response.json().get("features", []))

    payload = {"type": "FeatureCollection", "features": features}
    _write_geojson(cache_path, payload)
    return payload


def _load_arcgis_geojson(raw_dir, cache_name, url, fixtures_dir=None, fixture_name=None, bbox=None):
    if fixtures_dir and fixture_name:
        return _read_geojson(Path(fixtures_dir) / fixture_name)

    cache_path = raw_dir / cache_name
    if cache_path.exists():
        return _read_geojson(cache_path)

    params = {
        "f": "geojson",
        "where": "1=1",
        "outFields": "*",
        "outSR": 4326,
    }
    if bbox:
        params.update(
            {
                "geometry": ",".join(str(value) for value in bbox),
                "geometryType": "esriGeometryEnvelope",
                "inSR": 4326,
                "spatialRel": "esriSpatialRelIntersects",
            }
        )

    response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    _write_geojson(cache_path, payload)
    return payload


def load_redevelopment_areas(raw_dir, bbox, fixtures_dir=None, redevelopment_url=None):
    return _load_arcgis_geojson(
        raw_dir=raw_dir,
        cache_name="newark-redevelopment-areas.geojson",
        url=redevelopment_url,
        fixtures_dir=fixtures_dir,
        fixture_name="redevelopment_areas.geojson",
        bbox=bbox,
    )


def load_transit_stops(
    raw_dir,
    bbox,
    fixtures_dir=None,
    light_rail_url=None,
    nj_transit_station_url=None,
    path_station_url=None,
):
    if fixtures_dir:
        return _read_geojson(Path(fixtures_dir) / "transit_stops.geojson")

    cache_path = raw_dir / "newark-transit-stops.geojson"
    if cache_path.exists():
        return _read_geojson(cache_path)

    feature_collection = {"type": "FeatureCollection", "features": []}
    for cache_name, url in (
        ("newark-light-rail-stops.geojson", light_rail_url),
        ("newark-nj-transit-stops.geojson", nj_transit_station_url),
        ("newark-path-stops.geojson", path_station_url),
    ):
        payload = _load_arcgis_geojson(
            raw_dir=raw_dir,
            cache_name=cache_name,
            url=url,
            bbox=bbox,
        )
        feature_collection["features"].extend(payload.get("features", []))

    _write_geojson(cache_path, feature_collection)
    return feature_collection


def load_overture_features(raw_dir, bbox, feature_type, fixtures_dir=None, overture_cli=None):
    if fixtures_dir:
        return _read_geojson(Path(fixtures_dir) / ("%ss.geojson" % feature_type if feature_type != "address" else "addresses.geojson"))

    filename = "overture-%ss.geojson" % feature_type if feature_type != "address" else "overture-addresses.geojson"
    cache_path = raw_dir / filename
    if cache_path.exists():
        return _read_geojson(cache_path)

    bbox_value = ",".join(str(value) for value in bbox)
    cli_candidates = []
    resolved_cli = shutil.which(overture_cli or "overturemaps")
    if resolved_cli:
        cli_candidates.append(resolved_cli)
    cli_candidates.append(str(Path.home() / "Library" / "Python" / "3.9" / "bin" / "overturemaps"))
    cli_candidates.append(overture_cli or "overturemaps")

    commands = [
        [
            cli_candidate,
            "download",
            "--bbox=%s" % bbox_value,
            "-f",
            "geojson",
            "--type=%s" % feature_type,
            "-o",
            str(cache_path),
        ]
        for cli_candidate in cli_candidates
    ] + [
        [
            sys.executable,
            "-m",
            "overturemaps",
            "download",
            "--bbox=%s" % bbox_value,
            "-f",
            "geojson",
            "--type=%s" % feature_type,
            "-o",
            str(cache_path),
        ],
    ]
    last_error = None
    for command in commands:
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
            last_error = None
            break
        except FileNotFoundError as exc:
            last_error = exc
        except subprocess.CalledProcessError as exc:
            last_error = exc
            if command[0] == sys.executable:
                break
    if last_error:
        raise RuntimeError(
            "Overture download failed. Ensure the overturemaps dependency is installed and callable."
        ) from last_error

    return _read_geojson(cache_path)


def _market_bbox(boundary_feature):
    if boundary_feature["geometry"]["type"] == "Polygon":
        coordinates = boundary_feature["geometry"]["coordinates"][0]
    else:
        coordinates = boundary_feature["geometry"]["coordinates"][0][0]
    longitudes = [coordinate[0] for coordinate in coordinates]
    latitudes = [coordinate[1] for coordinate in coordinates]
    return [min(longitudes), min(latitudes), max(longitudes), max(latitudes)]


def _source_document_records(run_id, now, boundary_url, parcels_url, redevelopment_url, transit_url):
    access_date = now.date().isoformat()
    return {
        "boundary": {
            "id": "%s-boundary" % run_id,
            "source_name": "census_tiger_place",
            "title": "Census TIGER place boundary for Newark",
            "source_url": boundary_url,
            "access_date": access_date,
            "document_type": "boundary_extract",
            "parser_version": PARSER_VERSION,
        },
        "parcels": {
            "id": "%s-parcels" % run_id,
            "source_name": "njgin_parcels",
            "title": "NJGIN Newark parcels and MOD-IV extract",
            "source_url": parcels_url,
            "access_date": access_date,
            "document_type": "parcel_extract",
            "parser_version": PARSER_VERSION,
        },
        "buildings": {
            "id": "%s-buildings" % run_id,
            "source_name": "overture_buildings",
            "title": "Overture building extract for Newark",
            "source_url": "https://docs.overturemaps.org/getting-data/overturemaps-py/",
            "access_date": access_date,
            "document_type": "building_extract",
            "parser_version": PARSER_VERSION,
        },
        "addresses": {
            "id": "%s-addresses" % run_id,
            "source_name": "overture_addresses",
            "title": "Overture address extract for Newark",
            "source_url": "https://docs.overturemaps.org/getting-data/overturemaps-py/",
            "access_date": access_date,
            "document_type": "address_extract",
            "parser_version": PARSER_VERSION,
        },
        "places": {
            "id": "%s-places" % run_id,
            "source_name": "overture_places",
            "title": "Overture place extract for Newark",
            "source_url": "https://docs.overturemaps.org/getting-data/overturemaps-py/",
            "access_date": access_date,
            "document_type": "place_extract",
            "parser_version": PARSER_VERSION,
        },
        "redevelopment": {
            "id": "%s-redevelopment" % run_id,
            "source_name": "newark_redevelopment_areas",
            "title": "Newark redevelopment plan areas",
            "source_url": redevelopment_url,
            "access_date": access_date,
            "document_type": "redevelopment_extract",
            "parser_version": PARSER_VERSION,
        },
        "transit": {
            "id": "%s-transit" % run_id,
            "source_name": "nj_transit_rail_network",
            "title": "NJ Transit and PATH station extract for Newark",
            "source_url": transit_url,
            "access_date": access_date,
            "document_type": "transit_extract",
            "parser_version": PARSER_VERSION,
        },
    }


def _normalize_parcel_features(feature_collection):
    parcels = []
    for feature in feature_collection.get("features", []):
        properties = feature.get("properties", {})
        parcel_pin = properties.get("PAMS_PIN") or properties.get("PIN_NODUP") or properties.get("GIS_PIN")
        if not parcel_pin:
            continue
        parcels.append(
            {
                "id": "parcel-%s" % _slugify(parcel_pin),
                "parcel_pin": parcel_pin,
                "county": properties.get("COUNTY") or "Essex",
                "municipality": properties.get("MUN_NAME") or "Newark",
                "block": str(properties.get("PCLBLOCK") or ""),
                "lot": str(properties.get("PCLLOT") or ""),
                "qualifier": properties.get("PCLQCODE"),
                "property_location": properties.get("PROP_LOC") or properties.get("ST_ADDRESS"),
                "owner_name": properties.get("OWNER_NAME"),
                "property_class_code": str(properties.get("PROP_CLASS") or "").upper(),
                "property_use_code": properties.get("PROP_USE"),
                "land_description": properties.get("LAND_DESC"),
                "zoning_code": None,
                "land_value": _maybe_float(properties.get("LAND_VAL")) or 0.0,
                "improvement_value": _maybe_float(properties.get("IMPRVT_VAL")) or 0.0,
                "sale_price": _maybe_float(properties.get("SALE_PRICE")),
                "sale_date": _parse_sale_date(properties.get("DEED_DATE")),
                "year_built": _maybe_int(properties.get("YR_CONSTR")),
                "calculated_acres": _maybe_float(properties.get("CALC_ACRE")),
                "geometry": feature.get("geometry"),
            }
        )
    return parcels


def _normalize_building_features(feature_collection):
    buildings = []
    for feature in feature_collection.get("features", []):
        properties = feature.get("properties", {})
        external_id = feature.get("id") or properties.get("id")
        if not external_id or not feature.get("geometry"):
            continue
        buildings.append(
            {
                "id": "building-%s" % _slugify(str(external_id)),
                "external_id": str(external_id),
                "building_name": _extract_feature_name(properties),
                "height_m": _maybe_float(properties.get("height")),
                "geometry": feature.get("geometry"),
            }
        )
    return buildings


def _normalize_address_features(feature_collection):
    addresses = []
    for feature in feature_collection.get("features", []):
        properties = feature.get("properties", {})
        external_id = feature.get("id") or properties.get("id") or str(uuid.uuid4())
        if not feature.get("geometry"):
            continue
        addresses.append(
            {
                "id": "address-%s" % _slugify(str(external_id)),
                "external_id": str(external_id),
                "display_name": _extract_address_display_name(properties),
                "geometry": feature.get("geometry"),
            }
        )
    return addresses


def _normalize_place_features(feature_collection):
    places = []
    for feature in feature_collection.get("features", []):
        properties = feature.get("properties", {})
        external_id = feature.get("id") or properties.get("id") or str(uuid.uuid4())
        if not feature.get("geometry"):
            continue
        places.append(
            {
                "id": "place-%s" % _slugify(str(external_id)),
                "geometry": feature.get("geometry"),
            }
        )
    return places


def _normalize_redevelopment_features(feature_collection):
    areas = []
    for feature in feature_collection.get("features", []):
        properties = feature.get("properties", {})
        name = properties.get("Name")
        if not name or not feature.get("geometry"):
            continue
        identifier = properties.get("OBJECTID") or name
        areas.append(
            {
                "id": "redevelopment-%s" % _slugify(str(identifier)),
                "name": name.strip(),
                "short_name": (properties.get("ShortName") or "").strip() or None,
                "plan_link": (properties.get("Link") or "").strip() or None,
                "geometry": feature.get("geometry"),
            }
        )
    return areas


def _normalize_transit_features(feature_collection):
    stops = []
    for feature in feature_collection.get("features", []):
        properties = feature.get("properties", {})
        stop_name = properties.get("STATION") or properties.get("STATION_ID") or properties.get("LOCATION")
        rail_line = properties.get("RAIL_LINE") or properties.get("RAIL_ID")
        if not stop_name or not feature.get("geometry"):
            continue
        external_id = (
            properties.get("ATIS_ID")
            or properties.get("OBJECTID")
            or properties.get("ID")
            or stop_name
        )
        stop_type = "Transit"
        if properties.get("RAIL_LINE"):
            stop_type = "Light Rail"
        elif properties.get("RAIL_ID") == "PATH":
            stop_type = "PATH"
        elif properties.get("RAIL_ID"):
            stop_type = "NJ Transit Rail"
        stops.append(
            {
                "id": "transit-%s" % _slugify(str(external_id)),
                "stop_name": str(stop_name).strip(),
                "stop_type": stop_type,
                "rail_line": str(rail_line).strip() if rail_line else None,
                "municipality": (properties.get("MUNICIPALI") or properties.get("LOCATION") or "").strip() or None,
                "county": (properties.get("COUNTY") or "").strip() or None,
                "geometry": feature.get("geometry"),
            }
        )
    return stops


def _upsert_source_documents(cursor, source_documents):
    rows = [
        (
            document["id"],
            document["source_name"],
            document["title"],
            document["source_url"],
            document["access_date"],
            document["document_type"],
            document["parser_version"],
        )
        for document in source_documents.values()
    ]
    cursor.executemany(
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
        rows,
    )


def _upsert_market(cursor, market_id, market_name, market_state, boundary_feature, source_document_id, now):
    cursor.execute(
        """
        INSERT INTO markets (id, name, state, source_document_id, last_ingested_at, geom)
        VALUES (
            %s,
            %s,
            %s,
            %s,
            %s,
            ST_Multi(ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4269), 4326))
        )
        ON CONFLICT (id) DO UPDATE SET
            name = EXCLUDED.name,
            state = EXCLUDED.state,
            source_document_id = EXCLUDED.source_document_id,
            last_ingested_at = EXCLUDED.last_ingested_at,
            geom = EXCLUDED.geom
        """,
        (
            market_id,
            market_name,
            market_state,
            source_document_id,
            now,
            json.dumps(boundary_feature["geometry"]),
        ),
    )


def _upsert_parcels(cursor, market_id, parcels, source_document_id, now):
    rows = [
        (
            item["id"],
            market_id,
            item["parcel_pin"],
            item["county"],
            item["municipality"],
            item["block"],
            item["lot"],
            item["qualifier"],
            item["property_location"],
            item["owner_name"],
            item["property_class_code"],
            item["property_use_code"],
            item["land_description"],
            item["zoning_code"],
            item["land_value"],
            item["improvement_value"],
            item["sale_price"],
            item["sale_date"],
            item["year_built"],
            item["calculated_acres"],
            source_document_id,
            now,
            json.dumps(item["geometry"]),
        )
        for item in parcels
    ]
    cursor.executemany(
        """
        INSERT INTO parcels (
            id, market_id, parcel_pin, county, municipality, block, lot, qualifier,
            property_location, owner_name, property_class_code, property_use_code,
            land_description, zoning_code, land_value, improvement_value, sale_price,
            sale_date, year_built, calculated_acres, source_document_id, last_ingested_at, geom
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))
        )
        ON CONFLICT (id) DO UPDATE SET
            market_id = EXCLUDED.market_id,
            parcel_pin = EXCLUDED.parcel_pin,
            county = EXCLUDED.county,
            municipality = EXCLUDED.municipality,
            block = EXCLUDED.block,
            lot = EXCLUDED.lot,
            qualifier = EXCLUDED.qualifier,
            property_location = EXCLUDED.property_location,
            owner_name = EXCLUDED.owner_name,
            property_class_code = EXCLUDED.property_class_code,
            property_use_code = EXCLUDED.property_use_code,
            land_description = EXCLUDED.land_description,
            zoning_code = EXCLUDED.zoning_code,
            land_value = EXCLUDED.land_value,
            improvement_value = EXCLUDED.improvement_value,
            sale_price = EXCLUDED.sale_price,
            sale_date = EXCLUDED.sale_date,
            year_built = EXCLUDED.year_built,
            calculated_acres = EXCLUDED.calculated_acres,
            source_document_id = EXCLUDED.source_document_id,
            last_ingested_at = EXCLUDED.last_ingested_at,
            geom = EXCLUDED.geom
        """,
        rows,
    )
    cursor.execute(
        "DELETE FROM parcels WHERE market_id = %s AND NOT (id = ANY(%s))",
        (market_id, [item["id"] for item in parcels]),
    )


def _upsert_buildings(cursor, market_id, buildings, source_document_id, now):
    rows = [
        (
            item["id"],
            market_id,
            item["external_id"],
            item["building_name"],
            item["height_m"],
            source_document_id,
            now,
            json.dumps(item["geometry"]),
        )
        for item in buildings
    ]
    cursor.executemany(
        """
        INSERT INTO buildings (
            id, market_id, external_id, building_name, height_m,
            source_document_id, last_ingested_at, geom
        )
        VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))
        )
        ON CONFLICT (id) DO UPDATE SET
            market_id = EXCLUDED.market_id,
            external_id = EXCLUDED.external_id,
            building_name = EXCLUDED.building_name,
            height_m = EXCLUDED.height_m,
            source_document_id = EXCLUDED.source_document_id,
            last_ingested_at = EXCLUDED.last_ingested_at,
            geom = EXCLUDED.geom
        """,
        rows,
    )
    cursor.execute(
        "DELETE FROM buildings WHERE market_id = %s AND NOT (id = ANY(%s))",
        (market_id, [item["id"] for item in buildings]),
    )


def _upsert_addresses(cursor, market_id, addresses, source_document_id, now):
    rows = [
        (
            item["id"],
            market_id,
            item["external_id"],
            item["display_name"],
            source_document_id,
            now,
            json.dumps(item["geometry"]),
        )
        for item in addresses
    ]
    cursor.executemany(
        """
        INSERT INTO addresses (
            id, market_id, external_id, display_name,
            source_document_id, last_ingested_at, geom
        )
        VALUES (
            %s, %s, %s, %s,
            %s, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)
        )
        ON CONFLICT (id) DO UPDATE SET
            market_id = EXCLUDED.market_id,
            external_id = EXCLUDED.external_id,
            display_name = EXCLUDED.display_name,
            source_document_id = EXCLUDED.source_document_id,
            last_ingested_at = EXCLUDED.last_ingested_at,
            geom = EXCLUDED.geom
        """,
        rows,
    )
    cursor.execute(
        "DELETE FROM addresses WHERE market_id = %s AND NOT (id = ANY(%s))",
        (market_id, [item["id"] for item in addresses]),
    )


def _upsert_redevelopment_areas(cursor, market_id, areas, source_document_id, now):
    rows = [
        (
            item["id"],
            market_id,
            item["name"],
            item["short_name"],
            item["plan_link"],
            source_document_id,
            now,
            json.dumps(item["geometry"]),
        )
        for item in areas
    ]
    if rows:
        cursor.executemany(
            """
            INSERT INTO redevelopment_areas (
                id, market_id, name, short_name, plan_link,
                source_document_id, last_ingested_at, geom
            )
            VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))
            )
            ON CONFLICT (id) DO UPDATE SET
                market_id = EXCLUDED.market_id,
                name = EXCLUDED.name,
                short_name = EXCLUDED.short_name,
                plan_link = EXCLUDED.plan_link,
                source_document_id = EXCLUDED.source_document_id,
                last_ingested_at = EXCLUDED.last_ingested_at,
                geom = EXCLUDED.geom
            """,
            rows,
        )
    cursor.execute(
        "DELETE FROM redevelopment_areas WHERE market_id = %s AND NOT (id = ANY(%s))",
        (market_id, [item["id"] for item in areas]),
    )


def _upsert_transit_stops(cursor, market_id, transit_stops, source_document_id, now):
    rows = [
        (
            item["id"],
            market_id,
            item["stop_name"],
            item["stop_type"],
            item["rail_line"],
            item["municipality"],
            item["county"],
            source_document_id,
            now,
            json.dumps(item["geometry"]),
        )
        for item in transit_stops
    ]
    if rows:
        cursor.executemany(
            """
            INSERT INTO transit_stops (
                id, market_id, stop_name, stop_type, rail_line, municipality, county,
                source_document_id, last_ingested_at, geom
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s,
                %s, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)
            )
            ON CONFLICT (id) DO UPDATE SET
                market_id = EXCLUDED.market_id,
                stop_name = EXCLUDED.stop_name,
                stop_type = EXCLUDED.stop_type,
                rail_line = EXCLUDED.rail_line,
                municipality = EXCLUDED.municipality,
                county = EXCLUDED.county,
                source_document_id = EXCLUDED.source_document_id,
                last_ingested_at = EXCLUDED.last_ingested_at,
                geom = EXCLUDED.geom
            """,
            rows,
        )
    cursor.execute(
        "DELETE FROM transit_stops WHERE market_id = %s AND NOT (id = ANY(%s))",
        (market_id, [item["id"] for item in transit_stops]),
    )


def _mark_interrupted_runs_failed(cursor, market_id, now):
    cursor.execute(
        """
        UPDATE ingest_runs
        SET status = 'failed',
            completed_at = %s,
            qa_report = jsonb_build_object(
                'passed', false,
                'message', 'Superseded or interrupted before completion'
            )
        WHERE market_id = %s
          AND status = 'running'
        """,
        (now, market_id),
    )


def _reset_fixture_tables(cursor):
    cursor.execute(
        """
        TRUNCATE TABLE
            audit_log,
            organization_roles,
            parcel_ownership_claims,
            recorder_documents,
            people,
            organizations,
            addresses,
            buildings,
            redevelopment_areas,
            transit_stops,
            parcels,
            markets,
            ingest_runs,
            source_documents
        RESTART IDENTITY CASCADE
        """
    )


def _fixture_reset_safe(cursor, market_id):
    cursor.execute(
        "SELECT COUNT(*) AS count FROM parcels WHERE market_id = %s",
        (market_id,),
    )
    return int(cursor.fetchone()["count"]) <= FIXTURE_RESET_GUARD_PARCEL_LIMIT


def _refresh_building_spatial_relationships(cursor, market_id):
    cursor.execute(
        """
        UPDATE parcels
        SET geom = ST_Multi(ST_MakeValid(geom))
        WHERE market_id = %s AND NOT ST_IsValid(geom)
        """,
        (market_id,),
    )
    cursor.execute(
        """
        UPDATE buildings
        SET geom = ST_Multi(ST_MakeValid(geom))
        WHERE market_id = %s AND NOT ST_IsValid(geom)
        """,
        (market_id,),
    )
    cursor.execute(
        "UPDATE buildings SET parcel_id = NULL, place_count = 0, building_sqft = NULL WHERE market_id = %s",
        (market_id,),
    )
    cursor.execute("ANALYZE parcels")
    cursor.execute("ANALYZE buildings")
    cursor.execute(
        """
        UPDATE buildings b
        SET parcel_id = p.id
        FROM parcels p
        WHERE b.market_id = %s
          AND p.market_id = b.market_id
          AND ST_Contains(p.geom, b.centroid)
        """,
        (market_id,),
    )
    cursor.execute(
        """
        UPDATE buildings
        SET building_sqft = ROUND((ST_Area(geom::geography) * 10.7639)::numeric, 2)
        WHERE market_id = %s
        """,
        (market_id,),
    )


def _refresh_address_relationships(cursor, market_id):
    cursor.execute(
        "UPDATE addresses SET building_id = NULL, parcel_id = NULL WHERE market_id = %s",
        (market_id,),
    )
    cursor.execute("ANALYZE addresses")
    cursor.execute(
        """
        UPDATE addresses a
        SET parcel_id = p.id
        FROM parcels p
        WHERE a.market_id = %s
          AND p.market_id = a.market_id
          AND ST_Contains(p.geom, a.geom)
        """,
        (market_id,),
    )
    cursor.execute(
        """
        UPDATE addresses a
        SET building_id = b.id
        FROM buildings b
        WHERE a.market_id = %s
          AND b.market_id = a.market_id
          AND b.parcel_id = a.parcel_id
          AND ST_Contains(b.geom, a.geom)
        """,
        (market_id,),
    )
    cursor.execute(
        """
        WITH nearest_building_matches AS (
            SELECT
                a.id AS address_id,
                candidate.building_id
            FROM addresses a
            CROSS JOIN LATERAL (
                SELECT b.id AS building_id
                FROM buildings b
                WHERE b.market_id = a.market_id
                  AND b.parcel_id = a.parcel_id
                  AND ST_DWithin(b.centroid::geography, a.geom::geography, 40)
                ORDER BY ST_Distance(b.centroid::geography, a.geom::geography), b.id
                LIMIT 1
            ) AS candidate
            WHERE a.market_id = %s
              AND a.parcel_id IS NOT NULL
              AND a.building_id IS NULL
        )
        UPDATE addresses a
        SET building_id = nearest_building_matches.building_id
        FROM nearest_building_matches
        WHERE a.id = nearest_building_matches.address_id
        """,
        (market_id,),
    )
    cursor.execute(
        """
        UPDATE addresses a
        SET parcel_id = b.parcel_id
        FROM buildings b
        WHERE a.market_id = %s
          AND a.building_id = b.id
          AND a.parcel_id IS NULL
        """,
        (market_id,),
    )


def _apply_place_counts(cursor, market_id, places):
    cursor.execute("DROP TABLE IF EXISTS ingest_places")
    cursor.execute(
        """
        CREATE TEMP TABLE ingest_places (
            id TEXT PRIMARY KEY,
            geom geometry(Point, 4326) NOT NULL
        ) ON COMMIT DROP
        """
    )
    if places:
        rows = [(item["id"], json.dumps(item["geometry"])) for item in places]
        cursor.executemany(
            """
            INSERT INTO ingest_places (id, geom)
            VALUES (%s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))
            """,
            rows,
        )
    cursor.execute("CREATE INDEX ingest_places_geom_idx ON ingest_places USING GIST (geom)")
    cursor.execute(
        """
        DELETE FROM ingest_places ip
        USING markets m
        WHERE m.id = %s
          AND NOT ST_Intersects(m.geom, ip.geom)
        """,
        (market_id,),
    )
    cursor.execute("ANALYZE ingest_places")

    cursor.execute("UPDATE buildings SET place_count = 0 WHERE market_id = %s", (market_id,))
    cursor.execute(
        """
        WITH counts AS (
            SELECT b.id, COUNT(ip.id) AS count
            FROM buildings b
            JOIN ingest_places ip ON ST_Intersects(b.geom, ip.geom)
            WHERE b.market_id = %s
            GROUP BY b.id
        )
        UPDATE buildings b
        SET place_count = counts.count
        FROM counts
        WHERE b.id = counts.id
        """,
        (market_id,),
    )

    cursor.execute(
        """
        UPDATE parcels
        SET building_count = 0,
            place_count = 0,
            building_sqft_total = 0
        WHERE market_id = %s
        """,
        (market_id,),
    )
    cursor.execute(
        """
        WITH building_rollup AS (
            SELECT parcel_id, COUNT(*) AS building_count, COALESCE(SUM(building_sqft), 0) AS sqft_total
            FROM buildings
            WHERE market_id = %s AND parcel_id IS NOT NULL
            GROUP BY parcel_id
        )
        UPDATE parcels p
        SET building_count = building_rollup.building_count,
            building_sqft_total = building_rollup.sqft_total
        FROM building_rollup
        WHERE p.id = building_rollup.parcel_id
        """,
        (market_id,),
    )
    cursor.execute(
        """
        WITH place_rollup AS (
            SELECT p.id AS parcel_id, COUNT(ip.id) AS place_count
            FROM parcels p
            JOIN ingest_places ip ON ST_Intersects(p.geom, ip.geom)
            WHERE p.market_id = %s
            GROUP BY p.id
        )
        UPDATE parcels p
        SET place_count = place_rollup.place_count
        FROM place_rollup
        WHERE p.id = place_rollup.parcel_id
        """,
        (market_id,),
    )


def _apply_classification_and_scores(cursor, market_id):
    cursor.execute(
        """
        UPDATE parcels
        SET classification = CASE UPPER(property_class_code)
            WHEN '4A' THEN 'COMMERCIAL'::property_classification
            WHEN '4B' THEN 'INDUSTRIAL'::property_classification
            WHEN '4C' THEN 'MULTIFAMILY'::property_classification
            WHEN '2' THEN 'RESIDENTIAL'::property_classification
            WHEN '1' THEN 'VACANT'::property_classification
            WHEN '3A' THEN 'FARM'::property_classification
            WHEN '3B' THEN 'FARM'::property_classification
            WHEN '15A' THEN 'EXEMPT_PUBLIC'::property_classification
            WHEN '15B' THEN 'EXEMPT_PUBLIC'::property_classification
            WHEN '15C' THEN 'EXEMPT_PUBLIC'::property_classification
            WHEN '15D' THEN 'EXEMPT_RELIGIOUS_CHARITABLE'::property_classification
            WHEN '15E' THEN 'EXEMPT_CEMETERY'::property_classification
            WHEN '15F' THEN 'EXEMPT_OTHER'::property_classification
            WHEN '5A' THEN 'RAILROAD'::property_classification
            WHEN '5B' THEN 'RAILROAD'::property_classification
            ELSE 'UNKNOWN'::property_classification
        END
        WHERE market_id = %s
        """,
        (market_id,),
    )
    cursor.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                CASE
                    WHEN COUNT(*) OVER () = 1 THEN 100.0
                    ELSE ROUND(((PERCENT_RANK() OVER (ORDER BY total_assessed_value)) * 100.0)::numeric, 2)
                END AS percentile
            FROM parcels
            WHERE market_id = %s
        )
        UPDATE parcels p
        SET assessed_value_percentile = ranked.percentile
        FROM ranked
        WHERE p.id = ranked.id
        """,
        (market_id,),
    )
    cursor.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                CASE
                    WHEN COUNT(*) OVER () = 1 THEN 100.0
                    ELSE ROUND(((PERCENT_RANK() OVER (ORDER BY building_sqft_total)) * 100.0)::numeric, 2)
                END AS percentile
            FROM parcels
            WHERE market_id = %s
        )
        UPDATE parcels p
        SET building_sqft_percentile = ranked.percentile
        FROM ranked
        WHERE p.id = ranked.id
        """,
        (market_id,),
    )
    cursor.execute(
        """
        UPDATE parcels
        SET class_relevance_score = CASE classification
            WHEN 'COMMERCIAL' THEN 100.0
            WHEN 'MULTIFAMILY' THEN 100.0
            WHEN 'INDUSTRIAL' THEN 65.0
            WHEN 'EXEMPT_PUBLIC' THEN 55.0
            WHEN 'EXEMPT_RELIGIOUS_CHARITABLE' THEN 60.0
            WHEN 'EXEMPT_OTHER' THEN 25.0
            WHEN 'RAILROAD' THEN 15.0
            WHEN 'VACANT' THEN 10.0
            WHEN 'FARM' THEN 5.0
            WHEN 'EXEMPT_CEMETERY' THEN 5.0
            ELSE 0.0
        END,
        place_density_signal = LEAST(place_count::numeric, 10.0) / 10.0 * 100.0
        WHERE market_id = %s
        """,
        (market_id,),
    )
    cursor.execute(
        """
        UPDATE parcels
        SET priority_score = ROUND(
                (COALESCE(assessed_value_percentile, 0) * 0.55)
              + (COALESCE(class_relevance_score, 0) * 0.30)
              + (COALESCE(building_sqft_percentile, 0) * 0.10)
              + (COALESCE(place_density_signal, 0) * 0.05),
            2
        )
        WHERE market_id = %s
        """,
        (market_id,),
    )
    cursor.execute(
        """
        UPDATE parcels
        SET priority_tier = CASE
            WHEN priority_score >= 80 THEN 'Tier 1'::priority_tier
            WHEN priority_score >= 60 THEN 'Tier 2'::priority_tier
            WHEN priority_score >= 40 THEN 'Tier 3'::priority_tier
            ELSE 'Tier 4'::priority_tier
        END
        WHERE market_id = %s
        """,
        (market_id,),
    )


def _run_quality_checks(cursor, market_id):
    qa = {"checks": []}

    def append_check(name, value, threshold, comparator, fail_message):
        passed = comparator(value, threshold)
        qa["checks"].append(
            {
                "name": name,
                "value": value,
                "threshold": threshold,
                "passed": passed,
                "message": fail_message if not passed else "ok",
            }
        )

    cursor.execute(
        "SELECT COUNT(*) AS count FROM parcels WHERE market_id = %s AND NOT ST_IsValid(geom)",
        (market_id,),
    )
    append_check(
        "invalid_parcel_geometries",
        int(cursor.fetchone()["count"]),
        0,
        lambda value, threshold: value <= threshold,
        "Parcel geometries remain invalid after repair",
    )

    cursor.execute(
        """
        SELECT COUNT(*) AS count
        FROM (
            SELECT parcel_pin
            FROM parcels
            WHERE market_id = %s
            GROUP BY parcel_pin
            HAVING COUNT(*) > 1
        ) duplicates
        """,
        (market_id,),
    )
    append_check(
        "duplicate_parcel_pins",
        int(cursor.fetchone()["count"]),
        0,
        lambda value, threshold: value <= threshold,
        "Duplicate parcel pins found in Newark dataset",
    )

    cursor.execute(
        """
        SELECT COALESCE(
            ROUND(
                (COUNT(*) FILTER (WHERE property_class_code IS NULL OR property_class_code = ''))::numeric
                / NULLIF(COUNT(*), 0) * 100.0,
                2
            ),
            0
        ) AS rate
        FROM parcels
        WHERE market_id = %s
        """,
        (market_id,),
    )
    append_check(
        "null_class_code_rate",
        float(cursor.fetchone()["rate"]),
        5.0,
        lambda value, threshold: value <= threshold,
        "Null property class code rate exceeds 5%",
    )

    cursor.execute(
        """
        SELECT COUNT(*) AS count
        FROM parcels p
        JOIN markets m ON m.id = p.market_id
        WHERE p.market_id = %s
          AND NOT ST_Within(p.centroid, m.geom)
        """,
        (market_id,),
    )
    append_check(
        "parcels_outside_boundary",
        int(cursor.fetchone()["count"]),
        5,
        lambda value, threshold: value <= threshold,
        "Parcels fall outside Newark boundary",
    )

    cursor.execute(
        """
        SELECT
            COUNT(*) FILTER (
                WHERE b.parcel_id IS NULL
                  AND ST_Intersects(m.geom, b.centroid)
            ) AS unmatched_count,
            COUNT(*) FILTER (
                WHERE ST_Intersects(m.geom, b.centroid)
            ) AS total_count
        FROM buildings b
        JOIN markets m ON m.id = b.market_id
        WHERE b.market_id = %s
        """,
        (market_id,),
    )
    building_match_stats = cursor.fetchone()
    building_match_threshold = max(1000, (int(building_match_stats["total_count"]) + 9) // 10)
    append_check(
        "buildings_without_parcel",
        int(building_match_stats["unmatched_count"]),
        building_match_threshold,
        lambda value, threshold: value <= threshold,
        "Too many buildings failed to match a parcel",
    )

    cursor.execute(
        """
        SELECT
            COUNT(*) FILTER (
                WHERE building_id IS NULL
                  AND parcel_id IS NULL
                  AND ST_Intersects(m.geom, a.geom)
            ) AS unmatched_count,
            COUNT(*) FILTER (
                WHERE ST_Intersects(m.geom, a.geom)
            ) AS total_count
        FROM addresses a
        JOIN markets m ON m.id = a.market_id
        WHERE a.market_id = %s
        """,
        (market_id,),
    )
    address_match_stats = cursor.fetchone()
    address_match_threshold = max(100, (int(address_match_stats["total_count"]) + 4) // 5)
    append_check(
        "addresses_without_building_or_parcel",
        int(address_match_stats["unmatched_count"]),
        address_match_threshold,
        lambda value, threshold: value <= threshold,
        "Too many addresses failed to match a parcel or building",
    )

    qa["passed"] = all(check["passed"] for check in qa["checks"])
    return qa


def ingest_newark(fixtures_dir=None, progress=None, allow_fixture_reset=False):
    _log_step(progress, "Starting Newark ingest")
    settings = get_settings()
    now = _utc_now()
    run_id = _run_id()
    _log_step(progress, f"run_id={run_id}")
    source_documents = _source_document_records(
        run_id=run_id,
        now=now,
        boundary_url=settings.tiger_place_zip_url,
        parcels_url=settings.njgin_parcels_url,
        redevelopment_url=settings.redevelopment_areas_url,
        transit_url=settings.nj_transit_station_url,
    )
    _log_step(progress, "Loading source fixture/download inputs")

    boundary_feature = load_newark_boundary(
        raw_dir=settings.raw_dir,
        tmp_dir=settings.tmp_dir,
        fixtures_dir=fixtures_dir,
        tiger_place_zip_url=settings.tiger_place_zip_url,
    )
    _log_step(progress, "Boundary loaded")
    bbox = _market_bbox(boundary_feature)
    _log_step(progress, f"Bbox {bbox}")
    parcel_features = load_newark_parcels(
        raw_dir=settings.raw_dir,
        fixtures_dir=fixtures_dir,
        parcels_url=settings.njgin_parcels_url,
    )
    _log_step(progress, f"Parcels loaded: {len(parcel_features.get('features', []))}")
    building_features = load_overture_features(
        raw_dir=settings.raw_dir,
        bbox=bbox,
        feature_type="building",
        fixtures_dir=fixtures_dir,
        overture_cli=settings.overture_cli,
    )
    _log_step(progress, f"Buildings loaded: {len(building_features.get('features', []))}")
    address_features = {"type": "FeatureCollection", "features": []}
    if fixtures_dir or settings.include_overture_addresses:
        address_features = load_overture_features(
            raw_dir=settings.raw_dir,
            bbox=bbox,
            feature_type="address",
            fixtures_dir=fixtures_dir,
            overture_cli=settings.overture_cli,
        )
        _log_step(progress, f"Addresses loaded: {len(address_features.get('features', []))}")
    place_features = load_overture_features(
        raw_dir=settings.raw_dir,
        bbox=bbox,
        feature_type="place",
        fixtures_dir=fixtures_dir,
        overture_cli=settings.overture_cli,
    )
    redevelopment_features = load_redevelopment_areas(
        raw_dir=settings.raw_dir,
        bbox=bbox,
        fixtures_dir=fixtures_dir,
        redevelopment_url=settings.redevelopment_areas_url,
    )
    _log_step(progress, f"Redevelopment areas loaded: {len(redevelopment_features.get('features', []))}")
    transit_features = load_transit_stops(
        raw_dir=settings.raw_dir,
        bbox=bbox,
        fixtures_dir=fixtures_dir,
        light_rail_url=settings.nj_transit_light_rail_url,
        nj_transit_station_url=settings.nj_transit_station_url,
        path_station_url=settings.path_station_url,
    )
    _log_step(progress, f"Transit stops loaded: {len(transit_features.get('features', []))}")

    parcels = _normalize_parcel_features(parcel_features)
    buildings = _normalize_building_features(building_features)
    addresses = _normalize_address_features(address_features)
    places = _normalize_place_features(place_features)
    redevelopment_areas = _normalize_redevelopment_features(redevelopment_features)
    transit_stops = _normalize_transit_features(transit_features)
    _log_step(
        progress,
        f"Normalized rows: parcels={len(parcels)}, buildings={len(buildings)}, "
        f"addresses={len(addresses)}, places={len(places)}",
    )

    _log_step(progress, "Opening DB connection and ensuring schema")
    connection = get_connection(settings.database_url)
    qa_report = {}
    try:
        with advisory_job_lock(connection, LOCK_NAMESPACE_INGEST, settings.market_id):
            _log_step(progress, "Job lock acquired")
            ensure_schema(connection)

            with connection.cursor() as cursor:
                if fixtures_dir:
                    if not allow_fixture_reset and not _fixture_reset_safe(cursor, settings.market_id):
                        raise RuntimeError(
                            "Fixture ingest refused because Newark already has more than %s parcels loaded. "
                            "Use a dedicated test database or rerun with --allow-fixture-reset if you intend to replace the live dataset."
                            % FIXTURE_RESET_GUARD_PARCEL_LIMIT
                        )
                    _log_step(progress, "Fixture mode cleanup: truncating local tables")
                    _reset_fixture_tables(cursor)
                _mark_interrupted_runs_failed(cursor, settings.market_id, now)
                cursor.execute(
                    """
                    INSERT INTO ingest_runs (id, market_id, status, started_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (run_id, settings.market_id, "running", now),
                )
            connection.commit()
            _log_step(progress, "Ingest run marked as running")

            try:
                with connection.cursor() as cursor:
                    _log_step(progress, "Upserting source documents")
                    _upsert_source_documents(cursor, source_documents)
                    _log_step(progress, "Upserting market metadata")
                    _upsert_market(
                        cursor,
                        settings.market_id,
                        settings.market_name,
                        settings.market_state,
                        boundary_feature,
                        source_documents["boundary"]["id"],
                        now,
                    )
                    _log_step(progress, "Upserting parcels")
                    _upsert_parcels(cursor, settings.market_id, parcels, source_documents["parcels"]["id"], now)
                    _log_step(progress, "Upserting buildings")
                    _upsert_buildings(cursor, settings.market_id, buildings, source_documents["buildings"]["id"], now)
                    _log_step(progress, "Refreshing building relationships")
                    _refresh_building_spatial_relationships(cursor, settings.market_id)
                    if fixtures_dir or settings.include_overture_addresses:
                        _log_step(progress, "Upserting addresses")
                        _upsert_addresses(
                            cursor,
                            settings.market_id,
                            addresses,
                            source_documents["addresses"]["id"],
                            now,
                        )
                        _log_step(progress, "Refreshing address relationships")
                        _refresh_address_relationships(cursor, settings.market_id)
                    _log_step(progress, "Upserting redevelopment areas")
                    _upsert_redevelopment_areas(
                        cursor,
                        settings.market_id,
                        redevelopment_areas,
                        source_documents["redevelopment"]["id"],
                        now,
                    )
                    _log_step(progress, "Upserting transit stops")
                    _upsert_transit_stops(
                        cursor,
                        settings.market_id,
                        transit_stops,
                        source_documents["transit"]["id"],
                        now,
                    )
                    _log_step(progress, "Applying place counts")
                    _apply_place_counts(cursor, settings.market_id, places)
                    _log_step(progress, "Applying scores/classification")
                    _apply_classification_and_scores(cursor, settings.market_id)
                    _log_step(progress, "Running QA checks")
                    qa_report = _run_quality_checks(cursor, settings.market_id)
                    if not qa_report["passed"]:
                        _log_step(progress, "QA checks failed")
                        raise RuntimeError("Quality checks failed for Newark ingest")
                    cursor.execute(
                        """
                        UPDATE ingest_runs
                        SET status = %s,
                            completed_at = %s,
                            qa_report = %s
                        WHERE id = %s
                        """,
                        ("completed", _utc_now(), Json(qa_report), run_id),
                    )
                    _log_step(progress, "Run marked completed")
                connection.commit()
            except Exception:
                connection.rollback()
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE ingest_runs
                        SET status = %s,
                            completed_at = %s,
                            qa_report = %s
                        WHERE id = %s
                        """,
                        ("failed", _utc_now(), Json(qa_report or {"passed": False}), run_id),
                    )
                connection.commit()
                _log_step(progress, "Run marked failed")
                raise
    finally:
        connection.close()
        _log_step(progress, "Database connection closed")

    return {
        "run_id": run_id,
        "market_id": settings.market_id,
        "bbox": bbox,
        "parcels_loaded": len(parcels),
        "buildings_loaded": len(buildings),
        "addresses_loaded": len(addresses),
        "places_loaded": len(places),
        "redevelopment_areas_loaded": len(redevelopment_areas),
        "transit_stops_loaded": len(transit_stops),
        "qa_report": qa_report,
    }
