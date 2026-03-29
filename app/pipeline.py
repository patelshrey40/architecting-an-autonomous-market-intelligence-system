from __future__ import annotations

from copy import deepcopy

from app.demo_data import RAW_DATA


CLASSIFICATION_MAP = {
    "1": "VACANT",
    "2": "RESIDENTIAL",
    "3A": "FARM",
    "3B": "FARM",
    "4A": "COMMERCIAL",
    "4B": "INDUSTRIAL",
    "4C": "MULTIFAMILY",
    "5A": "RAILROAD",
    "5B": "RAILROAD",
    "15A": "EXEMPT_PUBLIC",
    "15B": "EXEMPT_PUBLIC",
    "15C": "EXEMPT_PUBLIC",
    "15D": "EXEMPT_RELIGIOUS_CHARITABLE",
    "15E": "EXEMPT_CEMETERY",
    "15F": "EXEMPT_OTHER",
}

CLASS_RELEVANCE_SCORES = {
    "COMMERCIAL": 100.0,
    "MULTIFAMILY": 100.0,
    "INDUSTRIAL": 65.0,
    "EXEMPT_PUBLIC": 55.0,
    "EXEMPT_RELIGIOUS_CHARITABLE": 60.0,
    "RESIDENTIAL": 0.0,
    "VACANT": 10.0,
    "FARM": 5.0,
    "RAILROAD": 15.0,
    "EXEMPT_CEMETERY": 5.0,
    "EXEMPT_OTHER": 25.0,
}

CLASS_COLORS = {
    "COMMERCIAL": "#e76f51",
    "MULTIFAMILY": "#f4a261",
    "INDUSTRIAL": "#264653",
    "EXEMPT_PUBLIC": "#6d597a",
    "EXEMPT_RELIGIOUS_CHARITABLE": "#7a9e7e",
    "RESIDENTIAL": "#8ab17d",
    "VACANT": "#d9d9d9",
    "UNKNOWN": "#bdb2ff",
}


def _index_by_id(records):
    return {record["id"]: record for record in records}


def _total_value(property_record):
    return property_record["land_value"] + property_record["improvement_value"]


def _clamp(value, minimum=0.0, maximum=100.0):
    return max(minimum, min(maximum, value))


def classify_property(property_record):
    code = property_record["property_class_code"].upper()
    primary = CLASSIFICATION_MAP.get(code, "UNKNOWN")
    flags = []

    zoning = property_record.get("zoning_code", "").upper()
    if primary == "COMMERCIAL" and zoning.startswith("MU"):
        flags.append("mixed_use_signal")
    if primary == "COMMERCIAL" and property_record.get("place_count", 0) >= 10:
        flags.append("commercial_multi_tenant")
    if primary == "RESIDENTIAL" and property_record.get("improvement_value", 0) > 5_000_000:
        flags.append("residential_value_anomaly")
    if primary.startswith("EXEMPT"):
        flags.append("nonprofit_validation_candidate")

    return {
        "primary": primary,
        "flags": flags,
        "color": CLASS_COLORS.get(primary, CLASS_COLORS["UNKNOWN"]),
    }


def _value_rank_scores(properties):
    per_city = {}
    for property_record in properties:
        per_city.setdefault(property_record["city"], []).append(_total_value(property_record))

    scores = {}
    for city, values in per_city.items():
        ordered = sorted(values)
        if len(ordered) == 1:
            city_scores = {ordered[0]: 100.0}
        else:
            city_scores = {
                value: (index / (len(ordered) - 1)) * 100.0
                for index, value in enumerate(ordered)
            }
        scores[city] = city_scores
    return scores


def _priority_tier(score):
    if score >= 80:
        return "Tier 1"
    if score >= 60:
        return "Tier 2"
    if score >= 40:
        return "Tier 3"
    return "Tier 4"


def _build_claim(
    claim_id,
    subject_type,
    subject_id,
    subject_label,
    predicate,
    object_type,
    object_id,
    object_label,
    source_document,
    confidence,
    valid_from=None,
    metadata=None,
):
    metadata = metadata or {}
    return {
        "id": claim_id,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "subject_label": subject_label,
        "predicate": predicate,
        "object_type": object_type,
        "object_id": object_id,
        "object_label": object_label,
        "confidence": confidence,
        "observed_at": source_document["access_date"],
        "valid_from": valid_from or source_document["access_date"],
        "source_document": {
            "id": source_document["id"],
            "title": source_document["title"],
            "source_name": source_document["source_name"],
            "document_type": source_document["document_type"],
            "access_date": source_document["access_date"],
            "url": source_document["url"],
        },
        "metadata": metadata,
    }


def build_demo_state():
    raw = deepcopy(RAW_DATA)
    properties = raw["properties"]
    organizations = _index_by_id(raw["organizations"])
    people = _index_by_id(raw["people"])
    corridors = _index_by_id(raw["corridors"])
    source_documents = _index_by_id(raw["source_documents"])
    projects = raw["projects"]

    ownership_by_property = {item["property_id"]: item for item in raw["ownership_links"]}
    financing_by_property = {}
    for item in raw["financing_links"]:
        financing_by_property.setdefault(item["property_id"], []).append(item)

    people_by_org = {}
    for item in raw["officer_links"]:
        people_by_org.setdefault(item["org_id"], []).append(item)

    speakers_by_person = {item["person_id"] for item in raw["speaker_links"]}
    projects_by_property = {}
    for project in projects:
        projects_by_property.setdefault(project["property_id"], []).append(project)

    value_rank_scores = _value_rank_scores(properties)
    enriched_properties = []
    property_details = {}
    claims = []

    for property_record in properties:
        classification = classify_property(property_record)
        corridor = corridors[property_record["corridor_id"]]
        total_value = _total_value(property_record)
        value_rank = value_rank_scores[property_record["city"]][total_value]
        permit_score = _clamp(property_record["permit_activity"] * 80.0 + (20.0 if property_record["redevelopment_area"] else 0.0))
        corridor_score = corridor["importance"] * 100.0
        transit_score = _clamp(100.0 - (property_record["transit_distance_m"] / 8.0))
        network_score = 100.0 if property_record["known_network_overlap"] else 0.0
        class_relevance = CLASS_RELEVANCE_SCORES.get(classification["primary"], 0.0)

        composite = round(
            (value_rank * 0.25)
            + (class_relevance * 0.25)
            + (permit_score * 0.20)
            + (corridor_score * 0.15)
            + (transit_score * 0.10)
            + (network_score * 0.05),
            1,
        )

        tier = _priority_tier(composite)
        owner_link = ownership_by_property[property_record["id"]]
        owner_org = organizations[owner_link["org_id"]]
        lender_links = financing_by_property.get(property_record["id"], [])
        property_people_links = list(people_by_org.get(owner_org["id"], []))
        property_people_links.extend(
            [
                link
                for lender_link in lender_links
                for link in people_by_org.get(lender_link["lender_org_id"], [])
            ]
        )
        property_people_links.extend(
            [
                link
                for link in raw["officer_links"]
                if organizations[link["org_id"]]["category"] == "government_agency"
                and organizations[link["org_id"]]["city"] == property_record["city"]
            ]
        )

        property_people = []
        seen_people_ids = set()
        for link in property_people_links:
            person = people[link["person_id"]]
            if person["id"] in seen_people_ids:
                continue
            seen_people_ids.add(person["id"])
            property_people.append(
                {
                    "id": person["id"],
                    "name": person["name"],
                    "title": link["title"],
                    "organization_name": organizations[link["org_id"]]["name"],
                    "email": person["email"],
                    "phone": person["phone"],
                    "role_type": person["role_type"],
                    "in_existing_network": person["in_existing_network"],
                    "prior_speaker": person["prior_speaker"],
                }
            )

        property_projects = []
        for project in projects_by_property.get(property_record["id"], []):
            property_projects.append(
                {
                    "id": project["id"],
                    "name": project["name"],
                    "status": project["status"],
                    "description": project["description"],
                    "key_date": project["key_date"],
                    "developer_name": organizations[project["developer_org_id"]]["name"],
                    "principal_name": people[project["principal_person_id"]]["name"],
                }
            )

        breakdown = {
            "assessed_value_rank": round(value_rank, 1),
            "property_class_relevance": round(class_relevance, 1),
            "permit_and_redevelopment": round(permit_score, 1),
            "corridor_importance": round(corridor_score, 1),
            "transit_proximity": round(transit_score, 1),
            "existing_network_overlap": round(network_score, 1),
        }

        property_card = {
            "id": property_record["id"],
            "city": property_record["city"],
            "name": property_record["name"],
            "address": property_record["address"],
            "parcel_pin": property_record["parcel_pin"],
            "corridor_name": corridor["name"],
            "classification": classification["primary"],
            "classification_color": classification["color"],
            "flags": classification["flags"],
            "priority_score": composite,
            "priority_tier": tier,
            "owner_name": owner_org["name"],
            "total_assessed_value": total_value,
            "lat": property_record["lat"],
            "lon": property_record["lon"],
            "project_count": len(property_projects),
            "people_count": len(property_people),
        }
        enriched_properties.append(property_card)

        owner_source_document = source_documents[owner_link["source_document_id"]]
        claims.append(
            _build_claim(
                claim_id=f"claim-owns-{property_record['id']}",
                subject_type="organization",
                subject_id=owner_org["id"],
                subject_label=owner_org["name"],
                predicate="ORGANIZATION_OWNS_PARCEL",
                object_type="parcel",
                object_id=property_record["id"],
                object_label=property_record["address"],
                source_document=owner_source_document,
                confidence=owner_link["confidence"],
                valid_from=owner_link["valid_from"],
            )
        )

        for officer_link in people_by_org.get(owner_org["id"], []):
            officer_source_document = source_documents[officer_link["source_document_id"]]
            person = people[officer_link["person_id"]]
            claims.append(
                _build_claim(
                    claim_id=f"claim-officer-{person['id']}-{owner_org['id']}",
                    subject_type="person",
                    subject_id=person["id"],
                    subject_label=person["name"],
                    predicate="PERSON_IS_OFFICER_OF_ORGANIZATION",
                    object_type="organization",
                    object_id=owner_org["id"],
                    object_label=owner_org["name"],
                    source_document=officer_source_document,
                    confidence=officer_link["confidence"],
                    valid_from=officer_link["start_date"],
                    metadata={"title": officer_link["title"]},
                )
            )

        for lender_link in lender_links:
            lender = organizations[lender_link["lender_org_id"]]
            lender_source_document = source_documents[lender_link["source_document_id"]]
            claims.append(
                _build_claim(
                    claim_id=f"claim-finance-{lender['id']}-{property_record['id']}",
                    subject_type="organization",
                    subject_id=lender["id"],
                    subject_label=lender["name"],
                    predicate="FUND_FINANCED_PARCEL",
                    object_type="parcel",
                    object_id=property_record["id"],
                    object_label=property_record["address"],
                    source_document=lender_source_document,
                    confidence=lender_link["confidence"],
                    valid_from=lender_link["recording_date"],
                    metadata={"amount": lender_link["amount"]},
                )
            )

        for project in projects_by_property.get(property_record["id"], []):
            project_source_document = source_documents[project["source_document_id"]]
            developer = organizations[project["developer_org_id"]]
            principal = people[project["principal_person_id"]]
            claims.append(
                _build_claim(
                    claim_id=f"claim-project-{project['id']}",
                    subject_type="organization",
                    subject_id=developer["id"],
                    subject_label=developer["name"],
                    predicate="ORGANIZATION_DEVELOPED_PROJECT",
                    object_type="project",
                    object_id=project["id"],
                    object_label=project["name"],
                    source_document=project_source_document,
                    confidence="probable",
                    valid_from=project["key_date"],
                )
            )
            claims.append(
                _build_claim(
                    claim_id=f"claim-principal-{project['id']}",
                    subject_type="person",
                    subject_id=principal["id"],
                    subject_label=principal["name"],
                    predicate="PERSON_IS_PRINCIPAL_OF_PROJECT",
                    object_type="project",
                    object_id=project["id"],
                    object_label=project["name"],
                    source_document=project_source_document,
                    confidence="probable",
                    valid_from=project["key_date"],
                )
            )

        detail_claims = [
            claim
            for claim in claims
            if claim["object_id"] == property_record["id"]
            or claim["subject_id"] == owner_org["id"]
            or any(person["id"] == claim["subject_id"] for person in property_people)
        ]

        property_details[property_record["id"]] = {
            "property": property_card,
            "parcel": {
                "pin": property_record["parcel_pin"],
                "block": property_record["block"],
                "lot": property_record["lot"],
                "county": property_record["county"],
                "property_class_code": property_record["property_class_code"],
                "zoning_code": property_record["zoning_code"],
                "land_value": property_record["land_value"],
                "improvement_value": property_record["improvement_value"],
                "total_assessed_value": total_value,
            },
            "building": {
                "name": property_record["name"],
                "square_feet": property_record["building_sqft"],
                "year_built": property_record["year_built"],
                "primary_classification": classification["primary"],
                "secondary_flags": classification["flags"],
            },
            "owner": {
                "id": owner_org["id"],
                "name": owner_org["name"],
                "category": owner_org["category"],
                "registered_agent": owner_org["registered_agent"],
                "formation_date": owner_org["formation_date"],
                "confidence": owner_link["confidence"],
                "valid_from": owner_link["valid_from"],
            },
            "lenders": [
                {
                    "name": organizations[item["lender_org_id"]]["name"],
                    "amount": item["amount"],
                    "recording_date": item["recording_date"],
                    "confidence": item["confidence"],
                }
                for item in lender_links
            ],
            "projects": property_projects,
            "people": property_people,
            "scoring_breakdown": breakdown,
            "source_documents": [
                source_documents[source_id] for source_id in property_record["source_document_ids"]
            ],
            "claims": sorted(
                detail_claims,
                key=lambda item: (item["observed_at"], item["id"]),
                reverse=True,
            ),
        }

    speaker_event_ids = {item["person_id"]: item["event_id"] for item in raw["speaker_links"]}
    property_score_lookup = {item["id"]: item["priority_score"] for item in enriched_properties}
    property_by_org = {}
    for link in raw["ownership_links"]:
        property_by_org.setdefault(link["org_id"], []).append(link["property_id"])
    financed_by_org = {}
    for link in raw["financing_links"]:
        financed_by_org.setdefault(link["lender_org_id"], []).append(link["property_id"])

    targets = []
    for person in raw["people"]:
        org_id = person["organization_id"]
        linked_property_ids = list(property_by_org.get(org_id, []))
        linked_property_ids.extend(financed_by_org.get(org_id, []))
        linked_property_ids.extend(
            [
                project["property_id"]
                for project in projects
                if project["principal_person_id"] == person["id"] or project["developer_org_id"] == org_id
            ]
        )

        if person["role_type"] == "civic":
            city_candidates = [
                item["id"]
                for item in enriched_properties
                if item["city"] == person["city"] and item["priority_tier"] in {"Tier 1", "Tier 2"}
            ]
            linked_property_ids.extend(city_candidates[:2])

        unique_property_ids = []
        seen_property_ids = set()
        for property_id in linked_property_ids:
            if property_id in seen_property_ids:
                continue
            seen_property_ids.add(property_id)
            unique_property_ids.append(property_id)

        linked_scores = [property_score_lookup[property_id] for property_id in unique_property_ids]
        influence_score = min(55.0, sum(linked_scores) / 2.5) if linked_scores else 0.0
        civic_bonus = 28.0 if person["role_type"] == "civic" else 0.0
        capital_bonus = 18.0 if person["role_type"] == "capital" and linked_scores else 0.0
        project_bonus = 12.0 if any(project["principal_person_id"] == person["id"] for project in projects) else 0.0
        warm_bonus = 6.0 if person["id"] in speakers_by_person else 0.0
        network_bonus = 10.0 if not person["in_existing_network"] else 2.0
        target_score = round(
            _clamp(influence_score + civic_bonus + capital_bonus + project_bonus + warm_bonus + network_bonus),
            1,
        )

        reasons = []
        if linked_scores:
            top_property = max(unique_property_ids, key=lambda item: property_score_lookup[item])
            top_property_name = property_details[top_property]["property"]["name"]
            reasons.append(f"linked to {top_property_name}")
        if person["role_type"] == "civic":
            reasons.append("holds a civic approval role in the market")
        if person["role_type"] == "capital":
            reasons.append("represents active financing exposure in the market")
        if person["id"] in speakers_by_person:
            event_name = next(
                event["name"]
                for event in raw["events"]
                if event["id"] == speaker_event_ids[person["id"]]
            )
            reasons.append(f"already appeared at {event_name}")
        if not person["in_existing_network"]:
            reasons.append("net-new outreach opportunity")

        organization = organizations[org_id]
        targets.append(
            {
                "id": person["id"],
                "name": person["name"],
                "title": person["primary_title"],
                "organization_name": organization["name"],
                "organization_category": organization["category"],
                "city": person["city"],
                "email": person["email"],
                "phone": person["phone"],
                "target_score": target_score,
                "linked_property_ids": unique_property_ids,
                "linked_property_count": len(unique_property_ids),
                "in_existing_network": person["in_existing_network"],
                "prior_speaker": person["id"] in speakers_by_person,
                "why_it_matters": "; ".join(reasons) if reasons else "market participant with limited current evidence",
            }
        )

    targets.sort(key=lambda item: item["target_score"], reverse=True)
    enriched_properties.sort(key=lambda item: item["priority_score"], reverse=True)

    city_summaries = []
    for city in sorted({item["city"] for item in enriched_properties}):
        city_properties = [item for item in enriched_properties if item["city"] == city]
        city_summaries.append(
            {
                "city": city,
                "property_count": len(city_properties),
                "tier_1_count": len([item for item in city_properties if item["priority_tier"] == "Tier 1"]),
                "avg_priority_score": round(
                    sum(item["priority_score"] for item in city_properties) / len(city_properties), 1
                ),
            }
        )

    summary = {
        "cities_covered": len(city_summaries),
        "property_count": len(enriched_properties),
        "tier_1_count": len([item for item in enriched_properties if item["priority_tier"] == "Tier 1"]),
        "claim_count": len(claims),
        "target_count": len(targets),
        "allowed_sources": len(
            [item for item in raw["source_adapters"] if item["mode"] in {"allowed", "allowed_with_caveat"}]
        ),
        "restricted_sources": len(
            [
                item
                for item in raw["source_adapters"]
                if item["mode"] in {"validation_only", "manual_only", "manual_or_partner_api"}
            ]
        ),
    }

    return {
        "title": raw["project_title"],
        "subtitle": raw["project_subtitle"],
        "phases": raw["phases"],
        "summary": summary,
        "city_summaries": city_summaries,
        "properties": enriched_properties,
        "property_details": property_details,
        "targets": targets,
        "claims": claims,
        "source_adapters": raw["source_adapters"],
        "source_documents": raw["source_documents"],
        "relationship_registry": raw["relationship_registry"],
    }
