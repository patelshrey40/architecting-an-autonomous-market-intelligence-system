from __future__ import annotations


PROMOTEABLE_PERSONAS = {"developer", "architect", "planner"}


def classify_persona(title, source_family, organization_name=None):
    title_value = (title or "").lower()
    organization_value = (organization_name or "").lower()
    source_value = (source_family or "").lower()

    if "architect" in title_value or "architecture" in organization_value or source_value == "license_record":
        return "architect"
    if any(token in title_value for token in ("planning", "zoning", "redevelopment", "council", "planner")):
        return "planner"
    if source_value in {"civic_roster", "public_directory"} and "architect" not in title_value:
        return "planner"
    return "developer"


def role_relevance_score(persona, title):
    title_value = (title or "").lower()
    if persona == "developer":
        if any(token in title_value for token in ("managing member", "principal", "partner", "president", "ceo", "founder")):
            return 95.0
        if any(token in title_value for token in ("member", "manager", "director", "officer")):
            return 82.0
        return 68.0
    if persona == "architect":
        if any(token in title_value for token in ("principal", "partner", "director")):
            return 94.0
        if "architect" in title_value:
            return 86.0
        return 72.0
    if persona == "planner":
        if any(token in title_value for token in ("chair", "director", "president")):
            return 92.0
        if any(token in title_value for token in ("member", "commissioner", "council", "planner", "zoning")):
            return 84.0
        return 70.0
    return 0.0


def market_relevance_score(linked_priority_scores, evidence_types):
    best_priority = max(linked_priority_scores or [0.0])
    evidence_type_set = set(evidence_types or [])
    evidence_bonus = min(20.0, float(len(evidence_type_set)) * 6.0)
    if "parcel_ownership" in evidence_type_set or "ownership_officer" in evidence_type_set:
        evidence_bonus = min(24.0, evidence_bonus + 8.0)
    if best_priority:
        return min(100.0, round(best_priority + evidence_bonus, 2))
    if "civic_roster" in evidence_type_set:
        return min(100.0, 85.0 + evidence_bonus)
    return min(100.0, 52.0 + evidence_bonus)


def contactability_score(contact_points):
    if not contact_points:
        return 0.0
    score = 0.0
    types = {item["contact_type"] for item in contact_points}
    if "email" in types:
        score += 52.0
    if "phone" in types:
        score += 22.0
    if "office_address" in types:
        score += 10.0
    if "license_record" in types:
        score += 12.0
    if "website_profile" in types:
        score += 18.0
    if len(types) >= 3:
        score += 8.0
    return min(100.0, round(score, 2))


def evidence_score(source_document_count, evidence_link_count, has_market_link):
    score = min(55.0, float(source_document_count) * 18.0)
    score += min(30.0, float(evidence_link_count) * 7.0)
    if has_market_link:
        score += 15.0
    return min(100.0, round(score, 2))


def compute_lead_score(role_score, market_score, contact_score, evidence_score_value):
    return round(
        (role_score * 0.40)
        + (market_score * 0.25)
        + (contact_score * 0.20)
        + (evidence_score_value * 0.15),
        2,
    )


def lead_status_for_score(score, has_contact, persona, has_market_link):
    if persona not in PROMOTEABLE_PERSONAS:
        return "rejected"
    if not has_contact:
        return "needs_review"
    if not has_market_link:
        return "needs_review"
    if score >= 72.0:
        return "auto_promote"
    if score >= 56.0:
        return "needs_review"
    return "partial"
