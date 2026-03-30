from __future__ import annotations

from datetime import date

from app.agent_runs import (
    VALIDATION_OUTCOME_FAILED,
    VALIDATION_OUTCOME_NEEDS_REVIEW,
    VALIDATION_OUTCOME_PASSED,
)
from app.ownership import CONFIDENCE_PROBABLE
from app.lead_scoring import PROMOTEABLE_PERSONAS
from app.lead_service import persist_lead_proposal
from app.ownership_service import persist_ownership_result


ALLOWED_MASTER_DECISIONS = {
    "accept",
    "reject",
    "needs_more_work",
    "needs_review",
    "partial",
    "conflict",
}


def validate_proposal(proposal_type, payload):
    if proposal_type == "master_decision":
        return _validate_master_decision(payload)
    if proposal_type == "ownership_promotion_bundle":
        return _validate_ownership_promotion_bundle(payload)
    if proposal_type == "lead_proposal":
        return _validate_lead_proposal(payload)
    return {
        "outcome": VALIDATION_OUTCOME_PASSED,
        "message": "Proposal structure is acceptable.",
        "payload": {},
    }


def _validate_master_decision(payload):
    decision = payload.get("decision")
    if decision not in ALLOWED_MASTER_DECISIONS:
        return {
            "outcome": VALIDATION_OUTCOME_FAILED,
            "message": "Master decision is outside the allowed decision set.",
            "payload": {"decision": decision, "allowed_decisions": sorted(ALLOWED_MASTER_DECISIONS)},
        }
    return {
        "outcome": VALIDATION_OUTCOME_PASSED,
        "message": "Master decision is well-formed.",
        "payload": {"decision": decision},
    }


def _validate_ownership_promotion_bundle(payload):
    recorder_result = payload.get("recorder_result") or {}
    decision = payload.get("decision")
    confidence_tier = payload.get("confidence_tier")
    issues = []
    warnings = []

    if decision not in ALLOWED_MASTER_DECISIONS:
        issues.append("promotion bundle decision is invalid")
    if confidence_tier != CONFIDENCE_PROBABLE:
        issues.append("ownership bundle confidence tier must remain capped at probable")
    if not recorder_result.get("source_name"):
        issues.append("recorder source name is required")
    if not recorder_result.get("access_date"):
        issues.append("recorder access date is required")
    if not isinstance(recorder_result.get("grouped_records") or [], list):
        issues.append("recorder grouped_records must be present")

    latest_deed = recorder_result.get("latest_deed")
    if latest_deed and latest_deed.get("document_category") not in {None, "deed"}:
        issues.append("latest deed must have deed document_category")

    if decision in {"needs_review", "conflict"}:
        warnings.append("bundle requires analyst review before promotion")
    if decision == "reject":
        warnings.append("bundle is not promotable")

    if issues:
        return {
            "outcome": VALIDATION_OUTCOME_FAILED,
            "message": "Ownership promotion bundle failed validation.",
            "payload": {"issues": issues, "warnings": warnings},
        }
    if warnings:
        return {
            "outcome": VALIDATION_OUTCOME_NEEDS_REVIEW,
            "message": "Ownership promotion bundle requires analyst review.",
            "payload": {"issues": issues, "warnings": warnings},
        }
    return {
        "outcome": VALIDATION_OUTCOME_PASSED,
        "message": "Ownership promotion bundle passed validation.",
        "payload": {"issues": issues, "warnings": warnings},
    }


def should_auto_promote(validation_outcome, payload):
    if validation_outcome != VALIDATION_OUTCOME_PASSED:
        return False
    if payload.get("persona"):
        return payload.get("review_decision") == "auto_promote"
    return payload.get("decision") in {"accept", "partial"}


def promote_proposal(connection, proposal):
    if proposal["proposal_type"] == "lead_proposal":
        return persist_lead_proposal(connection, proposal)
    if proposal["proposal_type"] != "ownership_promotion_bundle":
        raise ValueError("Unsupported proposal type %s for promotion" % proposal["proposal_type"])
    payload = proposal["payload"] or {}
    recorder_result = _hydrate_recorder_result(payload["recorder_result"])
    return persist_ownership_result(
        connection,
        payload["market_id"],
        payload["parcel_id"],
        recorder_result,
        payload.get("entity_result"),
    )


def _parse_iso_date(value):
    if not value or not isinstance(value, str):
        return value
    try:
        return date.fromisoformat(value)
    except ValueError:
        return value


def _hydrate_recorder_result(recorder_result):
    normalized = dict(recorder_result or {})
    grouped_records = []
    for record in normalized.get("grouped_records") or []:
        next_record = dict(record)
        next_record["recorded_at"] = _parse_iso_date(next_record.get("recorded_at"))
        grouped_records.append(next_record)
    normalized["grouped_records"] = grouped_records
    latest_deed = None
    if normalized.get("latest_deed"):
        latest_deed = dict(normalized["latest_deed"])
        latest_deed["recorded_at"] = _parse_iso_date(latest_deed.get("recorded_at"))
    normalized["latest_deed"] = latest_deed
    return normalized


def _validate_lead_proposal(payload):
    issues = []
    warnings = []
    if payload.get("persona") not in PROMOTEABLE_PERSONAS:
        issues.append("lead persona must be developer, architect, or planner")
    if not (payload.get("person") or {}).get("full_name"):
        issues.append("lead proposal person.full_name is required")
    if not payload.get("why_person_matters"):
        issues.append("lead proposal why_person_matters is required")
    if not payload.get("source_documents") and not payload.get("primary_source_document_id"):
        issues.append("lead proposal must include at least one source document reference")
    contacts = payload.get("contacts") or []
    if not contacts:
        warnings.append("lead proposal has no business contact points")
    evidence_links = payload.get("evidence_links") or []
    if not evidence_links:
        warnings.append("lead proposal is missing evidence links")
    if payload.get("lead_score", 0) < 72:
        warnings.append("lead score did not meet auto-promotion threshold")
    if issues:
        return {
            "outcome": VALIDATION_OUTCOME_FAILED,
            "message": "Lead proposal failed validation.",
            "payload": {"issues": issues, "warnings": warnings},
        }
    if warnings:
        return {
            "outcome": VALIDATION_OUTCOME_NEEDS_REVIEW,
            "message": "Lead proposal requires analyst review.",
            "payload": {"issues": issues, "warnings": warnings},
        }
    return {
        "outcome": VALIDATION_OUTCOME_PASSED,
        "message": "Lead proposal passed validation.",
        "payload": {"issues": issues, "warnings": warnings},
    }
