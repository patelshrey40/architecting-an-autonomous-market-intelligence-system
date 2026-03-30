from __future__ import annotations

import json

from psycopg.types.json import Json

from app.lead_scoring import (
    PROMOTEABLE_PERSONAS,
    classify_persona,
    compute_lead_score,
    contactability_score,
    evidence_score,
    lead_status_for_score,
    market_relevance_score,
    role_relevance_score,
)
from app.ownership import (
    CONFIDENCE_PROBABLE,
    _ensure_exact_named_organization,
    _slugify,
    _upsert_source_document,
    _utc_now,
    normalize_org_name,
)


LEAD_STATUS_PROMOTED = "promoted"
LEAD_STATUS_PARTIAL = "partial"
LEAD_STATUS_NEEDS_REVIEW = "needs_review"
LEAD_STATUS_REJECTED = "rejected"


def normalize_person_name(name):
    return " ".join((name or "").lower().replace(".", " ").split())


def build_developer_seed_candidates(connection, market_id, limit=100):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                p.id AS person_id,
                p.full_name,
                r.role_title,
                r.source_document_id AS role_source_document_id,
                o.id AS organization_id,
                o.name AS organization_name,
                o.source_document_id AS organization_source_document_id,
                sd.source_url AS organization_source_url,
                sd.title AS organization_source_title,
                c.parcel_id,
                pa.priority_score,
                pa.priority_tier
            FROM organization_roles r
            JOIN people p ON p.id = r.person_id
            JOIN organizations o ON o.id = r.organization_id
            LEFT JOIN source_documents sd ON sd.id = o.source_document_id
            LEFT JOIN parcel_ownership_claims c ON c.organization_id = o.id AND c.valid_to IS NULL
            LEFT JOIN parcels pa ON pa.id = c.parcel_id
            WHERE o.market_id = %s
            ORDER BY COALESCE(pa.priority_score, 0) DESC, p.full_name, r.role_title
            LIMIT %s
            """,
            (market_id, limit),
        )
        rows = cursor.fetchall()
    candidates = []
    for row in rows:
        candidates.append(
            {
                "seed_type": "ownership_officer",
                "source_family": "ownership_officer",
                "persona_hint": "developer",
                "person_id": row["person_id"],
                "full_name": row["full_name"],
                "title": row["role_title"],
                "organization_id": row["organization_id"],
                "organization_name": row["organization_name"],
                "parcel_ids": [row["parcel_id"]] if row["parcel_id"] else [],
                "priority_scores": [float(row["priority_score"] or 0.0)],
                "source_document_ids": [
                    item
                    for item in (row["role_source_document_id"], row["organization_source_document_id"])
                    if item
                ],
                "contacts": [
                    {
                        "contact_type": "website_profile",
                        "contact_value": row["organization_source_url"],
                        "label": row["organization_source_title"] or row["organization_name"],
                        "source_family": "business_registry",
                    }
                ]
                if row["organization_source_url"]
                else [],
            }
        )
    return candidates


def ensure_person(cursor, full_name, source_document_id, now, person_id=None, raw_payload=None):
    normalized_name = normalize_person_name(full_name)
    if person_id:
        resolved_person_id = person_id
    else:
        cursor.execute(
            """
            SELECT id
            FROM people
            WHERE normalized_name = %s
            ORDER BY last_enriched_at DESC NULLS LAST, id
            LIMIT 1
            """,
            (normalized_name,),
        )
        row = cursor.fetchone()
        resolved_person_id = row["id"] if row else "person-lead-%s" % _slugify(normalized_name)
    cursor.execute(
        """
        INSERT INTO people (id, full_name, normalized_name, source_document_id, last_enriched_at, raw_payload)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            full_name = EXCLUDED.full_name,
            normalized_name = EXCLUDED.normalized_name,
            source_document_id = EXCLUDED.source_document_id,
            last_enriched_at = EXCLUDED.last_enriched_at,
            raw_payload = people.raw_payload || EXCLUDED.raw_payload
        RETURNING id
        """,
        (
            resolved_person_id,
            full_name,
            normalized_name,
            source_document_id,
            now,
            Json(raw_payload or {}),
        ),
    )
    return cursor.fetchone()["id"]


def upsert_person_affiliation(
    cursor,
    market_id,
    person_id,
    organization_id,
    title,
    affiliation_type,
    persona,
    source_document_id,
    now,
    raw_payload=None,
):
    affiliation_id = "affiliation-%s-%s-%s-%s" % (
        _slugify(person_id),
        _slugify(organization_id or "org"),
        _slugify(title),
        _slugify(affiliation_type),
    )
    cursor.execute(
        """
        INSERT INTO person_affiliations (
            id,
            market_id,
            person_id,
            organization_id,
            title,
            affiliation_type,
            persona,
            confidence_tier,
            source_document_id,
            last_verified_at,
            raw_payload
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (person_id, organization_id, title, affiliation_type) DO UPDATE SET
            persona = EXCLUDED.persona,
            source_document_id = EXCLUDED.source_document_id,
            last_verified_at = EXCLUDED.last_verified_at,
            raw_payload = person_affiliations.raw_payload || EXCLUDED.raw_payload
        RETURNING id
        """,
        (
            affiliation_id,
            market_id,
            person_id,
            organization_id,
            title,
            affiliation_type,
            persona,
            CONFIDENCE_PROBABLE,
            source_document_id,
            now,
            Json(raw_payload or {}),
        ),
    )
    return cursor.fetchone()["id"]


def upsert_contact_point(
    cursor,
    market_id,
    person_id,
    organization_id,
    contact_type,
    contact_value,
    label,
    source_family,
    source_document_id,
    now,
    is_primary=False,
):
    if not contact_value:
        return None
    contact_id = "contact-%s-%s-%s" % (
        _slugify(person_id),
        _slugify(contact_type),
        _slugify(contact_value),
    )
    cursor.execute(
        """
        INSERT INTO contact_points (
            id,
            market_id,
            person_id,
            organization_id,
            contact_type,
            contact_value,
            label,
            source_family,
            source_document_id,
            confidence_tier,
            is_primary,
            last_verified_at,
            raw_payload
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (person_id, contact_type, contact_value) DO UPDATE SET
            label = EXCLUDED.label,
            source_family = EXCLUDED.source_family,
            source_document_id = EXCLUDED.source_document_id,
            is_primary = contact_points.is_primary OR EXCLUDED.is_primary,
            last_verified_at = EXCLUDED.last_verified_at
        RETURNING id
        """,
        (
            contact_id,
            market_id,
            person_id,
            organization_id,
            contact_type,
            contact_value,
            label,
            source_family,
            source_document_id,
            CONFIDENCE_PROBABLE,
            is_primary,
            now,
            Json({}),
        ),
    )
    return cursor.fetchone()["id"]


def persist_lead_proposal(connection, proposal):
    payload = proposal["payload"] or {}
    now = _utc_now()
    with connection.cursor() as cursor:
        source_documents = {}
        for document in payload.get("source_documents", []):
            _upsert_source_document(cursor, document)
            source_documents[document["id"]] = document

        person_payload = payload["person"]
        organization_payload = payload.get("organization")
        organization_id = None
        primary_source_document_id = payload.get("primary_source_document_id")

        if organization_payload:
            if organization_payload.get("organization_id"):
                organization_id = organization_payload["organization_id"]
            else:
                organization_id = _ensure_exact_named_organization(
                    cursor,
                    payload["market_id"],
                    organization_payload["name"],
                    primary_source_document_id,
                    now,
                )

        person_id = ensure_person(
            cursor,
            person_payload["full_name"],
            primary_source_document_id,
            now,
            person_id=person_payload.get("person_id"),
            raw_payload=person_payload,
        )

        for affiliation in payload.get("affiliations", []):
            upsert_person_affiliation(
                cursor,
                payload["market_id"],
                person_id,
                affiliation.get("organization_id") or organization_id,
                affiliation["title"],
                affiliation["affiliation_type"],
                payload["persona"],
                affiliation.get("source_document_id") or primary_source_document_id,
                now,
                raw_payload=affiliation,
            )

        contact_ids = []
        for index, contact in enumerate(payload.get("contacts", [])):
            contact_id = upsert_contact_point(
                cursor,
                payload["market_id"],
                person_id,
                contact.get("organization_id") or organization_id,
                contact["contact_type"],
                contact["contact_value"],
                contact.get("label"),
                contact.get("source_family"),
                contact.get("source_document_id") or primary_source_document_id,
                now,
                is_primary=index == 0,
            )
            if contact_id:
                contact_ids.append(contact_id)

        lead_id = payload.get("lead_id") or "lead-%s-%s" % (
            _slugify(payload["market_id"]),
            _slugify("%s-%s" % (person_payload["full_name"], payload["persona"])),
        )
        cursor.execute(
            """
            INSERT INTO lead_candidates (
                id,
                market_id,
                person_id,
                organization_id,
                persona,
                lead_status,
                lead_score,
                role_relevance_score,
                market_relevance_score,
                contactability_score,
                evidence_score,
                review_state,
                why_person_matters,
                agent_run_id,
                source_document_id,
                last_verified_at,
                raw_payload,
                updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (market_id, person_id, persona) DO UPDATE SET
                organization_id = EXCLUDED.organization_id,
                lead_status = EXCLUDED.lead_status,
                lead_score = EXCLUDED.lead_score,
                role_relevance_score = EXCLUDED.role_relevance_score,
                market_relevance_score = EXCLUDED.market_relevance_score,
                contactability_score = EXCLUDED.contactability_score,
                evidence_score = EXCLUDED.evidence_score,
                review_state = EXCLUDED.review_state,
                why_person_matters = EXCLUDED.why_person_matters,
                agent_run_id = EXCLUDED.agent_run_id,
                source_document_id = EXCLUDED.source_document_id,
                last_verified_at = EXCLUDED.last_verified_at,
                raw_payload = EXCLUDED.raw_payload,
                updated_at = EXCLUDED.updated_at
            RETURNING id
            """,
            (
                lead_id,
                payload["market_id"],
                person_id,
                organization_id,
                payload["persona"],
                LEAD_STATUS_PROMOTED,
                payload["lead_score"],
                payload["role_relevance_score"],
                payload["market_relevance_score"],
                payload["contactability_score"],
                payload["evidence_score"],
                "approved",
                payload["why_person_matters"],
                proposal["agent_run_id"],
                primary_source_document_id,
                now,
                Json(payload),
                now,
            ),
        )
        lead_id = cursor.fetchone()["id"]

        cursor.execute("DELETE FROM lead_evidence_links WHERE lead_id = %s", (lead_id,))
        for evidence in payload.get("evidence_links", []):
            cursor.execute(
                """
                INSERT INTO lead_evidence_links (
                    id,
                    lead_id,
                    evidence_type,
                    parcel_id,
                    organization_id,
                    person_id,
                    source_document_id,
                    notes,
                    raw_payload
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    "evidence-%s-%s" % (lead_id, _slugify("%s-%s" % (evidence["evidence_type"], evidence.get("notes", "")))),
                    lead_id,
                    evidence["evidence_type"],
                    evidence.get("parcel_id"),
                    evidence.get("organization_id") or organization_id,
                    evidence.get("person_id") or person_id,
                    evidence.get("source_document_id") or primary_source_document_id,
                    evidence.get("notes"),
                    Json(evidence),
                ),
            )
    connection.commit()
    return {"lead_id": lead_id, "person_id": person_id, "contact_point_count": len(contact_ids)}


def synthesize_lead_payload(
    market_id,
    candidate,
    source_documents,
    llm_provider,
    tracker=None,
):
    source_document_ids = {
        document["id"]
        for document in (source_documents or [])
        if document.get("id")
    }
    source_document_ids.update(
        document_id for document_id in (candidate.get("source_document_ids") or []) if document_id
    )
    source_document_ids.update(
        contact.get("source_document_id")
        for contact in (candidate.get("contacts") or [])
        if contact.get("source_document_id")
    )
    source_document_ids.update(
        evidence.get("source_document_id")
        for evidence in (candidate.get("evidence_links") or [])
        if evidence.get("source_document_id")
    )
    persona = classify_persona(
        candidate.get("title"),
        candidate.get("source_family"),
        candidate.get("organization_name"),
    )
    role_score = role_relevance_score(persona, candidate.get("title"))
    market_score = market_relevance_score(candidate.get("priority_scores"), candidate.get("evidence_types"))
    contact_score = contactability_score(candidate.get("contacts", []))
    evidence_score_value = evidence_score(
        len(source_document_ids),
        len(candidate.get("evidence_links", [])),
        bool(candidate.get("has_market_link")),
    )
    lead_score = compute_lead_score(role_score, market_score, contact_score, evidence_score_value)
    status = lead_status_for_score(
        lead_score,
        bool(candidate.get("contacts")),
        persona,
        bool(candidate.get("has_market_link")),
    )
    heuristic_payload = {
        "why_person_matters": (
            "%s is tied to Newark market activity through %s and has public business contact evidence."
            % (
                candidate["full_name"],
                candidate.get("organization_name") or candidate.get("source_label") or candidate.get("title"),
            )
        ),
        "review_decision": status,
    }
    llm_payload = llm_provider.generate_structured(
        "lead_synthesis",
        {
            "type": "object",
            "properties": {
                "why_person_matters": {"type": "string"},
                "review_decision": {
                    "type": "string",
                    "enum": ["auto_promote", "needs_review", "partial", "conflict", "reject"],
                },
            },
            "required": ["why_person_matters", "review_decision"],
            "additionalProperties": False,
        },
        {
            "instructions": (
                "Summarize why this person matters to Newark market intelligence. "
                "Only use provided evidence. Prefer concise recruiter-readable language."
            ),
            "input": json.dumps(
                {
                    "candidate": candidate,
                    "scores": {
                        "role_relevance_score": role_score,
                        "market_relevance_score": market_score,
                        "contactability_score": contact_score,
                        "evidence_score": evidence_score_value,
                        "lead_score": lead_score,
                    },
                },
                indent=2,
            ),
            "fallback": heuristic_payload,
        },
    )
    decision = llm_payload.get("review_decision") or status
    if decision == "reject":
        decision = "needs_review"
    return {
        "market_id": market_id,
        "persona": persona,
        "lead_score": lead_score,
        "role_relevance_score": role_score,
        "market_relevance_score": market_score,
        "contactability_score": contact_score,
        "evidence_score": evidence_score_value,
        "lead_status": status,
        "review_decision": decision,
        "why_person_matters": llm_payload.get("why_person_matters") or heuristic_payload["why_person_matters"],
    }


def build_source_document(document_id, source_name, title, source_url, access_date, document_type, parser_version):
    return {
        "id": document_id,
        "source_name": source_name,
        "title": title,
        "source_url": source_url,
        "access_date": access_date or _utc_now().date().isoformat(),
        "document_type": document_type,
        "parser_version": parser_version,
    }
