from __future__ import annotations

from decimal import Decimal
import json

from app.agent_runs import (
    WORKFLOW_CONTACT_ENRICHMENT_V1,
    WORKFLOW_LEAD_SYNTHESIS_V1,
    WORKFLOW_OWNERSHIP_V1,
    get_latest_agent_run_for_entity,
)
from app.geojson import feature_collection, row_to_feature
from app.scoring import CLASSIFICATION_COLORS


MAX_PARCELS = 2000
MAX_BUILDINGS = 4000
MAX_TRANSIT_STOPS = 400
MAX_REDEVELOPMENT_AREAS = 100


def _recorder_category(document_type):
    normalized = (document_type or "").strip().upper()
    if normalized == "DEED":
        return "deed"
    if "ASSIGNMENT OF MORTGAGE" in normalized or ("ASSIGNMENT" in normalized and "MORTGAGE" in normalized):
        return "assignment_of_mortgage"
    if "MORTGAGE" in normalized:
        return "mortgage"
    if "LIS PENDENS" in normalized:
        return "lis_pendens"
    if "UCC" in normalized:
        return "ucc"
    if "LIEN" in normalized:
        return "lien"
    return "other"


def parse_bbox(bbox):
    if not bbox:
        return None
    parts = [part.strip() for part in bbox.split(",")]
    if len(parts) != 4:
        raise ValueError("bbox must contain minLon,minLat,maxLon,maxLat")
    values = tuple(float(part) for part in parts)
    min_lon, min_lat, max_lon, max_lat = values
    if min_lon >= max_lon or min_lat >= max_lat:
        raise ValueError("bbox coordinates are invalid")
    return values


def _to_float(value, default=0.0):
    if value is None:
        return default
    if isinstance(value, Decimal):
        return float(value)
    return value


def get_summary(connection, market_id):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                m.id,
                m.name,
                m.state,
                ST_AsGeoJSON(m.geom) AS boundary_geojson,
                ST_X(m.centroid) AS center_lon,
                ST_Y(m.centroid) AS center_lat,
                ST_XMin(m.geom) AS min_lon,
                ST_YMin(m.geom) AS min_lat,
                ST_XMax(m.geom) AS max_lon,
                ST_YMax(m.geom) AS max_lat,
                m.last_ingested_at,
                COUNT(p.id) AS property_count,
                COUNT(*) FILTER (WHERE p.priority_tier = 'Tier 1') AS tier_1_count,
                COALESCE(SUM(p.total_assessed_value), 0) AS total_assessed_value,
                COALESCE(AVG(p.priority_score), 0) AS average_priority_score,
                (
                    SELECT COUNT(*)
                    FROM addresses a
                    WHERE a.market_id = m.id
                      AND a.parcel_id IS NOT NULL
                ) AS address_count,
                (
                    SELECT COUNT(*)
                    FROM transit_stops t
                    WHERE t.market_id = m.id
                ) AS transit_stop_count,
                (
                    SELECT COUNT(*)
                    FROM redevelopment_areas r
                    WHERE r.market_id = m.id
                ) AS redevelopment_area_count,
                (
                    SELECT COUNT(*)
                    FROM lead_candidates l
                    WHERE l.market_id = m.id
                ) AS lead_count,
                (
                    SELECT COUNT(*)
                    FROM lead_candidates l
                    WHERE l.market_id = m.id
                      AND EXISTS (
                          SELECT 1
                          FROM contact_points cp
                          WHERE cp.person_id = l.person_id
                      )
                ) AS contactable_lead_count
            FROM markets m
            LEFT JOIN parcels p ON p.market_id = m.id
            WHERE m.id = %s
            GROUP BY m.id, m.name, m.state, m.geom, m.centroid, m.last_ingested_at
            """,
            (market_id,),
        )
        market = cursor.fetchone()
        if not market:
            return None

        cursor.execute(
            """
            SELECT classification::text AS classification, COUNT(*) AS count
            FROM parcels
            WHERE market_id = %s
            GROUP BY classification
            ORDER BY count DESC, classification
            """,
            (market_id,),
        )
        class_counts = cursor.fetchall()

        cursor.execute(
            """
            SELECT priority_tier::text AS priority_tier, COUNT(*) AS count
            FROM parcels
            WHERE market_id = %s
            GROUP BY priority_tier
            ORDER BY priority_tier
            """,
            (market_id,),
        )
        tier_counts = cursor.fetchall()

        return {
            "market_id": market["id"],
            "market_name": market["name"],
            "state": market["state"],
            "boundary_geojson": json.loads(market["boundary_geojson"]),
            "center": [_to_float(market["center_lat"]), _to_float(market["center_lon"])],
            "bbox": [
                _to_float(market["min_lon"]),
                _to_float(market["min_lat"]),
                _to_float(market["max_lon"]),
                _to_float(market["max_lat"]),
            ],
            "last_ingested_at": market["last_ingested_at"].isoformat() if market["last_ingested_at"] else None,
            "property_count": int(market["property_count"]),
            "tier_1_count": int(market["tier_1_count"]),
            "total_assessed_value": _to_float(market["total_assessed_value"]),
            "average_priority_score": round(_to_float(market["average_priority_score"]), 2),
            "address_count": int(market["address_count"]),
            "transit_stop_count": int(market["transit_stop_count"]),
            "redevelopment_area_count": int(market["redevelopment_area_count"]),
            "lead_count": int(market["lead_count"]),
            "contactable_lead_count": int(market["contactable_lead_count"]),
            "class_counts": [
                {"classification": item["classification"], "count": int(item["count"])}
                for item in class_counts
            ],
            "tier_counts": [
                {"priority_tier": item["priority_tier"], "count": int(item["count"])}
                for item in tier_counts
            ],
        }


def _parcel_where_clause(classification, tier, minimum_value, search_term, bbox):
    clauses = ["p.market_id = %(market_id)s"]
    params = {"classification": classification, "tier": tier, "minimum_value": minimum_value}
    if classification:
        clauses.append("p.classification::text = %(classification)s")
    if tier:
        clauses.append("p.priority_tier::text = %(tier)s")
    if minimum_value is not None:
        clauses.append("p.total_assessed_value >= %(minimum_value)s")
    if search_term:
        params["search_term"] = "%%%s%%" % search_term
        clauses.append(
            """
            (
                p.property_location ILIKE %(search_term)s
                OR p.parcel_pin ILIKE %(search_term)s
                OR EXISTS (
                    SELECT 1 FROM addresses a
                    WHERE a.parcel_id = p.id
                      AND a.display_name ILIKE %(search_term)s
                )
            )
            """
        )
    if bbox:
        min_lon, min_lat, max_lon, max_lat = bbox
        params.update(
            {
                "min_lon": min_lon,
                "min_lat": min_lat,
                "max_lon": max_lon,
                "max_lat": max_lat,
            }
        )
        clauses.append(
            "p.geom && ST_MakeEnvelope(%(min_lon)s, %(min_lat)s, %(max_lon)s, %(max_lat)s, 4326)"
        )
    return " AND ".join(clauses), params


def get_parcels(connection, market_id, bbox=None, classification=None, tier=None, minimum_value=None, search_term=None):
    where_clause, params = _parcel_where_clause(classification, tier, minimum_value, search_term, bbox)
    params["market_id"] = market_id
    tolerance = 0.00002
    if bbox:
        min_lon, min_lat, max_lon, max_lat = bbox
        tolerance = max((max_lon - min_lon) / 800.0, (max_lat - min_lat) / 800.0, 0.00001)
    params["tolerance"] = tolerance
    params["limit"] = MAX_PARCELS

    sql = """
        WITH filtered AS (
            SELECT
                p.id,
                p.parcel_pin,
                COALESCE(a.display_name, p.property_location, p.parcel_pin) AS display_name,
                p.classification::text AS classification,
                p.priority_tier::text AS priority_tier,
                p.priority_score,
                p.total_assessed_value,
                p.building_count,
                p.place_count,
                ST_AsGeoJSON(ST_SimplifyPreserveTopology(p.geom, %(tolerance)s)) AS geometry_json
            FROM parcels p
            LEFT JOIN LATERAL (
                SELECT display_name
                FROM addresses
                WHERE parcel_id = p.id
                ORDER BY display_name
                LIMIT 1
            ) a ON TRUE
            WHERE
    """ + where_clause + """
            ORDER BY p.priority_score DESC NULLS LAST, p.total_assessed_value DESC, p.id
            LIMIT %(limit)s
        )
        SELECT * FROM filtered
    """

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()

    features = []
    for row in rows:
        row["priority_score"] = _to_float(row["priority_score"])
        row["total_assessed_value"] = _to_float(row["total_assessed_value"])
        row["classification_color"] = CLASSIFICATION_COLORS.get(row["classification"], CLASSIFICATION_COLORS["UNKNOWN"])
        row["building_count"] = int(row["building_count"])
        row["place_count"] = int(row["place_count"])
        features.append(row_to_feature(row))
    return feature_collection(features)


def get_buildings(connection, market_id, bbox=None):
    params = {"market_id": market_id, "limit": MAX_BUILDINGS}
    clauses = ["market_id = %(market_id)s", "parcel_id IS NOT NULL"]
    if bbox:
        min_lon, min_lat, max_lon, max_lat = bbox
        params.update(
            {"min_lon": min_lon, "min_lat": min_lat, "max_lon": max_lon, "max_lat": max_lat}
        )
        clauses.append(
            "geom && ST_MakeEnvelope(%(min_lon)s, %(min_lat)s, %(max_lon)s, %(max_lat)s, 4326)"
        )

    sql = """
        SELECT
            id,
            parcel_id,
            COALESCE(building_name, id) AS display_name,
            COALESCE(building_sqft, 0) AS building_sqft,
            place_count,
            ST_AsGeoJSON(ST_SimplifyPreserveTopology(geom, 0.000005)) AS geometry_json
        FROM buildings
        WHERE
    """ + " AND ".join(clauses) + """
        ORDER BY building_sqft DESC NULLS LAST, id
        LIMIT %(limit)s
    """

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()

    features = []
    for row in rows:
        row["building_sqft"] = _to_float(row["building_sqft"])
        row["place_count"] = int(row["place_count"])
        features.append(row_to_feature(row))
    return feature_collection(features)


def get_transit_stops(connection, market_id, bbox=None):
    params = {"market_id": market_id, "limit": MAX_TRANSIT_STOPS}
    clauses = ["market_id = %(market_id)s"]
    if bbox:
        min_lon, min_lat, max_lon, max_lat = bbox
        params.update(
            {"min_lon": min_lon, "min_lat": min_lat, "max_lon": max_lon, "max_lat": max_lat}
        )
        clauses.append(
            "geom && ST_MakeEnvelope(%(min_lon)s, %(min_lat)s, %(max_lon)s, %(max_lat)s, 4326)"
        )

    sql = """
        SELECT
            id,
            stop_name AS display_name,
            stop_type,
            rail_line,
            municipality,
            county,
            ST_AsGeoJSON(geom) AS geometry_json
        FROM transit_stops
        WHERE
    """ + " AND ".join(clauses) + """
        ORDER BY stop_type, display_name
        LIMIT %(limit)s
    """

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()

    return feature_collection([row_to_feature(row) for row in rows])


def get_redevelopment_areas(connection, market_id, bbox=None):
    params = {"market_id": market_id, "limit": MAX_REDEVELOPMENT_AREAS}
    clauses = ["market_id = %(market_id)s"]
    if bbox:
        min_lon, min_lat, max_lon, max_lat = bbox
        params.update(
            {"min_lon": min_lon, "min_lat": min_lat, "max_lon": max_lon, "max_lat": max_lat}
        )
        clauses.append(
            "geom && ST_MakeEnvelope(%(min_lon)s, %(min_lat)s, %(max_lon)s, %(max_lat)s, 4326)"
        )

    sql = """
        SELECT
            id,
            name AS display_name,
            short_name,
            plan_link,
            ST_AsGeoJSON(geom) AS geometry_json
        FROM redevelopment_areas
        WHERE
    """ + " AND ".join(clauses) + """
        ORDER BY display_name
        LIMIT %(limit)s
    """

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()

    return feature_collection([row_to_feature(row) for row in rows])


def get_parcel_detail(connection, market_id, parcel_id):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                p.id,
                p.parcel_pin,
                p.block,
                p.lot,
                p.qualifier,
                COALESCE(a.display_name, p.property_location, p.parcel_pin) AS display_name,
                p.property_class_code,
                p.classification::text AS classification,
                p.priority_tier::text AS priority_tier,
                p.priority_score,
                p.total_assessed_value,
                p.land_value,
                p.improvement_value,
                p.building_count,
                p.place_count,
                p.building_sqft_total,
                p.assessed_value_percentile,
                p.class_relevance_score,
                p.building_sqft_percentile,
                p.place_density_signal,
                p.last_ingested_at,
                p.source_document_id,
                ST_AsGeoJSON(p.geom) AS geometry_json
            FROM parcels p
            LEFT JOIN LATERAL (
                SELECT display_name
                FROM addresses
                WHERE parcel_id = p.id
                ORDER BY display_name
                LIMIT 1
            ) a ON TRUE
            WHERE p.market_id = %s AND p.id = %s
            """,
            (market_id, parcel_id),
        )
        parcel = cursor.fetchone()
        if not parcel:
            return None

        cursor.execute(
            """
            SELECT
                id,
                COALESCE(building_name, id) AS building_name,
                COALESCE(building_sqft, 0) AS building_sqft,
                COALESCE(height_m, 0) AS height_m,
                place_count,
                ST_AsGeoJSON(geom) AS geometry_json
            FROM buildings
            WHERE parcel_id = %s
            ORDER BY building_sqft DESC NULLS LAST, id
            """,
            (parcel_id,),
        )
        buildings = cursor.fetchall()

        cursor.execute(
            """
            SELECT id, display_name
            FROM addresses
            WHERE parcel_id = %s
            ORDER BY display_name
            """,
            (parcel_id,),
        )
        addresses = cursor.fetchall()

        cursor.execute(
            """
            SELECT DISTINCT sd.*
            FROM source_documents sd
            WHERE sd.id = %s
               OR sd.id IN (
                   SELECT source_document_id FROM buildings WHERE parcel_id = %s
               )
               OR sd.id IN (
                   SELECT source_document_id FROM addresses WHERE parcel_id = %s
               )
               OR sd.id IN (
                   SELECT redevelopment_areas.source_document_id
                   FROM redevelopment_areas
                   WHERE market_id = %s
                     AND ST_Intersects(
                         geom,
                         (SELECT geom FROM parcels WHERE id = %s)
                     )
               )
               OR sd.id IN (
                   SELECT t.source_document_id
                   FROM transit_stops t
                   JOIN parcels p ON p.id = %s
                   WHERE t.market_id = p.market_id
               )
            ORDER BY sd.access_date DESC, sd.source_name
            """,
            (parcel["source_document_id"], parcel_id, parcel_id, market_id, parcel_id, parcel_id),
        )
        source_documents = cursor.fetchall()

        cursor.execute(
            """
            SELECT id, name, short_name, plan_link
            FROM redevelopment_areas
            WHERE market_id = %s
              AND ST_Intersects(
                  geom,
                  (SELECT geom FROM parcels WHERE id = %s)
              )
            ORDER BY name
            """,
            (market_id, parcel_id),
        )
        redevelopment_areas = cursor.fetchall()

        cursor.execute(
            """
            SELECT
                t.id,
                t.stop_name,
                t.stop_type,
                t.rail_line,
                ROUND(ST_Distance(t.geom::geography, p.centroid::geography)) AS distance_m
            FROM transit_stops t
            JOIN parcels p ON p.id = %s
            WHERE t.market_id = p.market_id
            ORDER BY ST_Distance(t.geom::geography, p.centroid::geography), t.stop_name
            LIMIT 5
            """,
            (parcel_id,),
        )
        nearby_transit = cursor.fetchall()

    return {
        "parcel": {
            "id": parcel["id"],
            "parcel_pin": parcel["parcel_pin"],
            "display_name": parcel["display_name"],
            "block": parcel["block"],
            "lot": parcel["lot"],
            "qualifier": parcel["qualifier"],
            "property_class_code": parcel["property_class_code"],
            "classification": parcel["classification"],
            "classification_color": CLASSIFICATION_COLORS.get(parcel["classification"], CLASSIFICATION_COLORS["UNKNOWN"]),
            "priority_tier": parcel["priority_tier"],
            "priority_score": _to_float(parcel["priority_score"]),
            "land_value": _to_float(parcel["land_value"]),
            "improvement_value": _to_float(parcel["improvement_value"]),
            "total_assessed_value": _to_float(parcel["total_assessed_value"]),
            "building_count": int(parcel["building_count"]),
            "place_count": int(parcel["place_count"]),
            "building_sqft_total": _to_float(parcel["building_sqft_total"]),
            "last_ingested_at": parcel["last_ingested_at"].isoformat() if parcel["last_ingested_at"] else None,
            "geometry": json.loads(parcel["geometry_json"]),
        },
        "score_breakdown": {
            "assessed_value_percentile": _to_float(parcel["assessed_value_percentile"]),
            "class_relevance_score": _to_float(parcel["class_relevance_score"]),
            "building_sqft_percentile": _to_float(parcel["building_sqft_percentile"]),
            "place_density_signal": _to_float(parcel["place_density_signal"]),
        },
        "buildings": [
            {
                "id": item["id"],
                "building_name": item["building_name"],
                "building_sqft": _to_float(item["building_sqft"]),
                "height_m": _to_float(item["height_m"]),
                "place_count": int(item["place_count"]),
                "geometry": json.loads(item["geometry_json"]),
            }
            for item in buildings
        ],
        "addresses": [dict(item) for item in addresses],
        "redevelopment_areas": [dict(item) for item in redevelopment_areas],
        "nearby_transit": [
            {
                "id": item["id"],
                "stop_name": item["stop_name"],
                "stop_type": item["stop_type"],
                "rail_line": item["rail_line"],
                "distance_m": int(item["distance_m"]),
            }
            for item in nearby_transit
        ],
        "sources": [
            {
                "id": item["id"],
                "source_name": item["source_name"],
                "title": item["title"],
                "source_url": item["source_url"],
                "access_date": item["access_date"].isoformat(),
                "document_type": item["document_type"],
            }
            for item in source_documents
        ],
    }


def get_parcel_ownership(connection, market_id, parcel_id):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                id,
                ownership_enrichment_status,
                ownership_last_enriched_at,
                ownership_note,
                ownership_metadata
            FROM parcels
            WHERE market_id = %s AND id = %s
            """,
            (market_id, parcel_id),
        )
        parcel = cursor.fetchone()
        if not parcel:
            return None
        latest_agent_run = get_latest_agent_run_for_entity(
            connection,
            market_id,
            "parcel",
            parcel_id,
            workflow_type=WORKFLOW_OWNERSHIP_V1,
        )

        cursor.execute(
            """
            SELECT
                c.id,
                c.owner_name,
                c.confidence_tier,
                c.valid_from,
                c.valid_to,
                c.notes,
                rd.id AS recorder_document_id,
                rd.document_number,
                rd.document_type,
                rd.document_category,
                rd.recorded_at,
                rd.book,
                rd.page,
                rd.grantors,
                rd.grantees,
                rd.consideration,
                o.id AS organization_id,
                o.name AS organization_name,
                o.nj_entity_id,
                o.matched_search_name,
                o.domicile_city,
                o.entity_type,
                o.entity_type_title,
                o.status AS organization_status,
                o.formation_date,
                o.registered_agent,
                o.source_document_id AS organization_source_document_id,
                rd.source_document_id AS recorder_source_document_id
            FROM parcel_ownership_claims c
            JOIN recorder_documents rd ON rd.id = c.recorder_document_id
            LEFT JOIN organizations o ON o.id = c.organization_id
            WHERE c.parcel_id = %s
            ORDER BY (c.valid_to IS NULL) DESC,
                     COALESCE(c.valid_from, DATE '1900-01-01') DESC,
                     c.created_at DESC
            """,
            (parcel_id,),
        )
        claims = cursor.fetchall()
        claim = claims[0] if claims else None
        active_claim_count = len([row for row in claims if row["valid_to"] is None])

        officers = []
        source_ids = set()
        related_filings = []
        deed_history = []
        recorder_history_last_enriched_at = None
        if claim and claim["recorder_source_document_id"]:
            source_ids.add(claim["recorder_source_document_id"])
        if claim and claim["organization_source_document_id"]:
            source_ids.add(claim["organization_source_document_id"])

        if claim and claim["organization_id"]:
            cursor.execute(
                """
                SELECT
                    p.id,
                    p.full_name,
                    r.role_title
                FROM organization_roles r
                JOIN people p ON p.id = r.person_id
                WHERE r.organization_id = %s
                ORDER BY p.full_name, r.role_title
                """,
                (claim["organization_id"],),
            )
            officers = cursor.fetchall()
            cursor.execute(
                """
                SELECT DISTINCT source_document_id
                FROM organization_roles
                WHERE organization_id = %s
                  AND source_document_id IS NOT NULL
                """,
                (claim["organization_id"],),
            )
            source_ids.update(row["source_document_id"] for row in cursor.fetchall())

        cursor.execute(
            """
            SELECT
                id,
                document_number,
                document_type,
                document_category,
                recorded_at,
                book,
                page,
                grantors,
                grantees,
                consideration,
                source_document_id,
                last_enriched_at
            FROM recorder_documents
            WHERE parcel_id = %s
            ORDER BY recorded_at DESC NULLS LAST, document_number DESC
            """,
            (parcel_id,),
        )
        all_recorder_documents = cursor.fetchall()
        for filing in all_recorder_documents:
            if filing["source_document_id"]:
                source_ids.add(filing["source_document_id"])
            if filing["last_enriched_at"] and (
                recorder_history_last_enriched_at is None
                or filing["last_enriched_at"] > recorder_history_last_enriched_at
            ):
                recorder_history_last_enriched_at = filing["last_enriched_at"]
            filing_payload = {
                "document_id": filing["id"],
                "document_number": filing["document_number"],
                "document_type": filing["document_type"],
                "document_category": filing["document_category"] or _recorder_category(filing["document_type"]),
                "recorded_at": filing["recorded_at"].isoformat() if filing["recorded_at"] else None,
                "book": filing["book"],
                "page": filing["page"],
                "grantors": filing["grantors"],
                "grantees": filing["grantees"],
                "consideration": _to_float(filing["consideration"], default=None),
            }
            if filing_payload["document_category"] == "deed":
                deed_history.append(filing_payload)
            else:
                related_filings.append(filing_payload)

        cursor.execute(
            """
            SELECT
                c.id,
                c.lender_name,
                c.claim_type,
                c.amount,
                c.confidence_tier,
                c.valid_from,
                c.valid_to,
                c.notes,
                rd.id AS recorder_document_id,
                rd.document_number,
                rd.document_type,
                rd.document_category,
                rd.recorded_at,
                rd.book,
                rd.page,
                rd.grantors,
                rd.grantees,
                o.id AS organization_id,
                o.name AS organization_name,
                o.nj_entity_id,
                o.matched_search_name,
                o.domicile_city,
                o.entity_type,
                o.entity_type_title,
                o.status AS organization_status,
                o.formation_date,
                o.registered_agent,
                c.source_document_id AS financing_source_document_id,
                o.source_document_id AS organization_source_document_id,
                rd.source_document_id AS recorder_source_document_id
            FROM parcel_financing_claims c
            JOIN recorder_documents rd ON rd.id = c.recorder_document_id
            LEFT JOIN organizations o ON o.id = c.organization_id
            WHERE c.parcel_id = %s
            ORDER BY rd.recorded_at DESC NULLS LAST, rd.document_number DESC, c.created_at DESC
            """,
            (parcel_id,),
        )
        financing_rows = cursor.fetchall()
        for financing_row in financing_rows:
            if financing_row["financing_source_document_id"]:
                source_ids.add(financing_row["financing_source_document_id"])
            if financing_row["organization_source_document_id"]:
                source_ids.add(financing_row["organization_source_document_id"])
            if financing_row["recorder_source_document_id"]:
                source_ids.add(financing_row["recorder_source_document_id"])

        sources = []
        if source_ids:
            cursor.execute(
                """
                SELECT id, source_name, title, source_url, access_date, document_type
                FROM source_documents
                WHERE id = ANY(%s)
                ORDER BY access_date DESC, source_name
                """,
                (list(source_ids),),
            )
            sources = cursor.fetchall()

        related_leads = get_leads(
            connection,
            market_id,
            parcel_id=parcel_id,
            limit=5,
        )["items"]

    metadata = parcel["ownership_metadata"] or {}
    review_reasons = list(metadata.get("review_reasons", []))
    qa = json.loads(json.dumps(metadata.get("qa", {}))) if metadata.get("qa") else {}
    enrichment_error = metadata.get("enrichment_error")
    provisional_latest_deed = metadata.get("latest_deed") or {}
    provisional_owner_name = metadata.get("owner_name")
    if claim and active_claim_count > 1:
        qa_issues = list(qa.get("issues", []))
        qa_issues.append(
            {
                "code": "duplicate_active_claims",
                "message": "Multiple active ownership claims exist for this parcel in storage.",
                "severity": "error",
            }
        )
        qa = {**qa, "issues": qa_issues}
        if "multiple active ownership claims" not in review_reasons:
            review_reasons.append("multiple active ownership claims were detected")

    return {
        "parcel_id": parcel["id"],
        "enrichment_status": parcel["ownership_enrichment_status"],
        "confidence_tier": claim["confidence_tier"] if claim else None,
        "last_enriched_at": (
            parcel["ownership_last_enriched_at"].isoformat()
            if parcel["ownership_last_enriched_at"]
            else None
        ),
        "active_claim_count": active_claim_count,
        "qa": qa,
        "review_reasons": review_reasons,
        "enrichment_error": enrichment_error,
        "message": parcel["ownership_note"],
        "recorder_history_last_enriched_at": (
            recorder_history_last_enriched_at.isoformat()
            if recorder_history_last_enriched_at
            else None
        ),
        "match_candidates": metadata.get("search_candidates", []),
        "agent_run": latest_agent_run,
        "deed": (
            {
                "claim_id": claim["id"],
                "document_number": claim["document_number"],
                "document_type": claim["document_type"],
                "document_category": claim["document_category"],
                "recorded_at": claim["recorded_at"].isoformat() if claim["recorded_at"] else None,
                "book": claim["book"],
                "page": claim["page"],
                "grantors": claim["grantors"],
                "grantees": claim["grantees"],
                "consideration": _to_float(claim["consideration"], default=None),
                "owner_name": claim["owner_name"],
                "valid_from": claim["valid_from"].isoformat() if claim["valid_from"] else None,
                "valid_to": claim["valid_to"].isoformat() if claim["valid_to"] else None,
                "notes": claim["notes"],
            }
            if claim
            else (
                {
                    "claim_id": None,
                    "document_number": provisional_latest_deed.get("document_number"),
                    "document_type": provisional_latest_deed.get("document_type"),
                    "document_category": provisional_latest_deed.get("document_category"),
                    "recorded_at": provisional_latest_deed.get("recorded_at"),
                    "book": provisional_latest_deed.get("book"),
                    "page": provisional_latest_deed.get("page"),
                    "grantors": provisional_latest_deed.get("grantors") or [],
                    "grantees": provisional_latest_deed.get("grantees") or [],
                    "consideration": _to_float(provisional_latest_deed.get("consideration"), default=None),
                    "owner_name": provisional_owner_name
                    or provisional_latest_deed.get("primary_grantee"),
                    "valid_from": provisional_latest_deed.get("recorded_at"),
                    "valid_to": None,
                    "notes": "Provisional owner summary from review-pending swarm output.",
                }
                if provisional_latest_deed or provisional_owner_name
                else None
            )
        ),
        "organization": (
            {
                "id": claim["organization_id"],
                "name": claim["organization_name"],
                "matched_search_name": claim["matched_search_name"],
                "matched_to_nj_entity": True,
                "nj_entity_id": claim["nj_entity_id"],
                "domicile_city": claim["domicile_city"],
                "entity_type": claim["entity_type"],
                "entity_type_title": claim["entity_type_title"],
                "status": claim["organization_status"],
                "formation_date": claim["formation_date"].isoformat() if claim["formation_date"] else None,
                "registered_agent": claim["registered_agent"],
            }
            if claim and claim["organization_id"]
            else (
                {
                    "id": None,
                    "name": claim["owner_name"],
                    "matched_search_name": None,
                    "matched_to_nj_entity": False,
                    "nj_entity_id": None,
                    "domicile_city": None,
                    "entity_type": None,
                    "entity_type_title": None,
                    "status": None,
                    "formation_date": None,
                    "registered_agent": None,
                }
                if claim
                else (
                    {
                        "id": None,
                        "name": provisional_owner_name
                        or provisional_latest_deed.get("primary_grantee"),
                        "matched_search_name": None,
                        "matched_to_nj_entity": False,
                        "nj_entity_id": None,
                        "domicile_city": None,
                        "entity_type": None,
                        "entity_type_title": None,
                        "status": None,
                        "formation_date": None,
                        "registered_agent": None,
                    }
                    if provisional_owner_name or provisional_latest_deed
                    else None
                )
            )
        ),
        "deed_history": deed_history,
        "financing_claims": [
            {
                "id": row["id"],
                "lender_name": row["lender_name"],
                "claim_type": row["claim_type"],
                "amount": _to_float(row["amount"], default=None),
                "confidence_tier": row["confidence_tier"],
                "valid_from": row["valid_from"].isoformat() if row["valid_from"] else None,
                "valid_to": row["valid_to"].isoformat() if row["valid_to"] else None,
                "notes": row["notes"],
                "document": {
                    "id": row["recorder_document_id"],
                    "document_number": row["document_number"],
                    "document_type": row["document_type"],
                    "document_category": row["document_category"]
                    or _recorder_category(row["document_type"]),
                    "recorded_at": row["recorded_at"].isoformat() if row["recorded_at"] else None,
                    "book": row["book"],
                    "page": row["page"],
                    "grantors": row["grantors"],
                    "grantees": row["grantees"],
                },
                "organization": (
                    {
                        "id": row["organization_id"],
                        "name": row["organization_name"],
                        "matched_search_name": row["matched_search_name"],
                        "matched_to_nj_entity": bool(row["nj_entity_id"]),
                        "nj_entity_id": row["nj_entity_id"],
                        "domicile_city": row["domicile_city"],
                        "entity_type": row["entity_type"],
                        "entity_type_title": row["entity_type_title"],
                        "status": row["organization_status"],
                        "formation_date": row["formation_date"].isoformat()
                        if row["formation_date"]
                        else None,
                        "registered_agent": row["registered_agent"],
                    }
                    if row["organization_id"]
                    else None
                ),
            }
            for row in financing_rows
        ],
        "related_filings": related_filings,
        "officers": [
            {"id": item["id"], "name": item["full_name"], "title": item["role_title"]}
            for item in officers
        ],
        "source_provenance": [
            {
                "id": item["id"],
                "source_name": item["source_name"],
                "title": item["title"],
                "source_url": item["source_url"],
                "access_date": item["access_date"].isoformat(),
                "document_type": item["document_type"],
            }
            for item in sources
        ],
        "related_leads": related_leads,
    }


def get_leads(
    connection,
    market_id,
    persona=None,
    score_min=None,
    contactable=None,
    org_id=None,
    parcel_id=None,
    search_term=None,
    limit=100,
):
    clauses = ["l.market_id = %(market_id)s"]
    params = {"market_id": market_id, "limit": limit}
    if persona:
        clauses.append("l.persona = %(persona)s")
        params["persona"] = persona
    if score_min is not None:
        clauses.append("l.lead_score >= %(score_min)s")
        params["score_min"] = score_min
    if org_id:
        clauses.append("l.organization_id = %(org_id)s")
        params["org_id"] = org_id
    if search_term:
        clauses.append(
            """
            (
                p.full_name ILIKE %(search_term)s
                OR COALESCE(o.name, '') ILIKE %(search_term)s
                OR COALESCE(l.why_person_matters, '') ILIKE %(search_term)s
            )
            """
        )
        params["search_term"] = "%%%s%%" % search_term
    if parcel_id:
        clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM lead_evidence_links lel
                WHERE lel.lead_id = l.id
                  AND lel.parcel_id = %(parcel_id)s
            )
            """
        )
        params["parcel_id"] = parcel_id
    if contactable:
        clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM contact_points cp
                WHERE cp.person_id = l.person_id
            )
            """
        )

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                l.id,
                l.persona,
                l.lead_status,
                l.lead_score,
                l.role_relevance_score,
                l.market_relevance_score,
                l.contactability_score,
                l.evidence_score,
                l.review_state,
                l.why_person_matters,
                l.last_verified_at,
                p.id AS person_id,
                p.full_name,
                o.id AS organization_id,
                o.name AS organization_name,
                pa.title AS current_title,
                (
                    SELECT jsonb_agg(
                        jsonb_build_object(
                            'contact_type', cp.contact_type,
                            'contact_value', cp.contact_value,
                            'label', cp.label,
                            'is_primary', cp.is_primary
                        )
                        ORDER BY cp.is_primary DESC, cp.contact_type, cp.contact_value
                    )
                    FROM contact_points cp
                    WHERE cp.person_id = l.person_id
                ) AS contacts_json
            FROM lead_candidates l
            JOIN people p ON p.id = l.person_id
            LEFT JOIN organizations o ON o.id = l.organization_id
            LEFT JOIN person_affiliations pa
              ON pa.person_id = l.person_id
             AND pa.organization_id IS NOT DISTINCT FROM l.organization_id
             AND pa.persona = l.persona
            WHERE
            """
            + " AND ".join(clauses)
            + """
            ORDER BY l.lead_score DESC, p.full_name
            LIMIT %(limit)s
            """,
            params,
        )
        rows = cursor.fetchall()
        items = []
        for row in rows:
            contacts = row["contacts_json"] or []
            items.append(
                {
                    "id": row["id"],
                    "persona": row["persona"],
                    "lead_status": row["lead_status"],
                    "lead_score": _to_float(row["lead_score"]),
                    "role_relevance_score": _to_float(row["role_relevance_score"]),
                    "market_relevance_score": _to_float(row["market_relevance_score"]),
                    "contactability_score": _to_float(row["contactability_score"]),
                    "evidence_score": _to_float(row["evidence_score"]),
                    "review_state": row["review_state"],
                    "why_person_matters": row["why_person_matters"],
                    "last_verified_at": row["last_verified_at"].isoformat()
                    if row["last_verified_at"]
                    else None,
                    "person": {
                        "id": row["person_id"],
                        "full_name": row["full_name"],
                    },
                    "organization": (
                        {"id": row["organization_id"], "name": row["organization_name"]}
                        if row["organization_id"]
                        else None
                    ),
                    "title": row["current_title"],
                    "contacts": contacts,
                }
            )
        latest_run = get_latest_agent_run_for_entity(
            connection,
            market_id,
            "market",
            market_id,
            workflow_type=WORKFLOW_LEAD_SYNTHESIS_V1,
        )
    return {"items": items, "latest_agent_run": latest_run}


def get_lead_detail(connection, market_id, lead_id):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                l.*,
                p.full_name,
                p.normalized_name,
                o.name AS organization_name
            FROM lead_candidates l
            JOIN people p ON p.id = l.person_id
            LEFT JOIN organizations o ON o.id = l.organization_id
            WHERE l.market_id = %s AND l.id = %s
            """,
            (market_id, lead_id),
        )
        lead = cursor.fetchone()
        if not lead:
            return None
        cursor.execute(
            """
            SELECT title, affiliation_type, persona, source_document_id
            FROM person_affiliations
            WHERE person_id = %s
            ORDER BY last_verified_at DESC NULLS LAST, title
            """,
            (lead["person_id"],),
        )
        affiliations = cursor.fetchall()
        cursor.execute(
            """
            SELECT contact_type, contact_value, label, source_family, is_primary, source_document_id
            FROM contact_points
            WHERE person_id = %s
            ORDER BY is_primary DESC, contact_type, contact_value
            """,
            (lead["person_id"],),
        )
        contacts = cursor.fetchall()
        cursor.execute(
            """
            SELECT evidence_type, parcel_id, organization_id, person_id, source_document_id, notes, raw_payload
            FROM lead_evidence_links
            WHERE lead_id = %s
            ORDER BY created_at, id
            """,
            (lead_id,),
        )
        evidence = cursor.fetchall()
        source_ids = {
            item["source_document_id"]
            for item in affiliations + contacts + evidence
            if item["source_document_id"]
        }
        if lead["source_document_id"]:
            source_ids.add(lead["source_document_id"])
        sources = []
        if source_ids:
            cursor.execute(
                """
                SELECT id, source_name, title, source_url, access_date, document_type
                FROM source_documents
                WHERE id = ANY(%s)
                ORDER BY access_date DESC, source_name, title
                """,
                (list(source_ids),),
            )
            sources = cursor.fetchall()
        latest_run = get_latest_agent_run_for_entity(
            connection,
            market_id,
            "person",
            lead["person_id"],
            workflow_type=WORKFLOW_CONTACT_ENRICHMENT_V1,
        )
    return {
        "id": lead["id"],
        "persona": lead["persona"],
        "lead_status": lead["lead_status"],
        "lead_score": _to_float(lead["lead_score"]),
        "role_relevance_score": _to_float(lead["role_relevance_score"]),
        "market_relevance_score": _to_float(lead["market_relevance_score"]),
        "contactability_score": _to_float(lead["contactability_score"]),
        "evidence_score": _to_float(lead["evidence_score"]),
        "review_state": lead["review_state"],
        "why_person_matters": lead["why_person_matters"],
        "last_verified_at": lead["last_verified_at"].isoformat() if lead["last_verified_at"] else None,
        "person": {
            "id": lead["person_id"],
            "full_name": lead["full_name"],
            "normalized_name": lead["normalized_name"],
        },
        "organization": (
            {"id": lead["organization_id"], "name": lead["organization_name"]}
            if lead["organization_id"]
            else None
        ),
        "affiliations": [
            {
                "title": item["title"],
                "affiliation_type": item["affiliation_type"],
                "persona": item["persona"],
                "source_document_id": item["source_document_id"],
            }
            for item in affiliations
        ],
        "contacts": [
            {
                "contact_type": item["contact_type"],
                "contact_value": item["contact_value"],
                "label": item["label"],
                "source_family": item["source_family"],
                "is_primary": item["is_primary"],
                "source_document_id": item["source_document_id"],
            }
            for item in contacts
        ],
        "evidence_links": [
            {
                "evidence_type": item["evidence_type"],
                "parcel_id": item["parcel_id"],
                "organization_id": item["organization_id"],
                "person_id": item["person_id"],
                "source_document_id": item["source_document_id"],
                "notes": item["notes"],
                "raw_payload": item["raw_payload"] or {},
            }
            for item in evidence
        ],
        "source_provenance": [
            {
                "id": item["id"],
                "source_name": item["source_name"],
                "title": item["title"],
                "source_url": item["source_url"],
                "access_date": item["access_date"].isoformat(),
                "document_type": item["document_type"],
            }
            for item in sources
        ],
        "agent_run": latest_run,
    }
