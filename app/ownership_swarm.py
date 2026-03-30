from __future__ import annotations

from dataclasses import dataclass
import re

from app.agent_registry import SwarmDefinition, register_swarm
from app.agent_runs import (
    PROPOSAL_STATUS_NEEDS_REVIEW,
    REVIEW_STATUS_PENDING,
    RUN_STATUS_IN_PROGRESS,
    TASK_STATUS_SKIPPED,
    VALIDATION_OUTCOME_FAILED,
    VALIDATION_OUTCOME_NEEDS_REVIEW,
    WORKFLOW_OWNERSHIP_V1,
)
from app.agent_validation import promote_proposal, should_auto_promote, validate_proposal
from app.ownership import (
    CONFIDENCE_PROBABLE,
    STATUS_FAILED,
    STATUS_NEEDS_REVIEW,
    choose_best_njbgs_match,
    extract_financing_claims,
    normalize_org_name,
)
from app.ownership_service import (
    mark_parcel_failed,
    mark_parcel_review_pending,
)
from app.ownership_tools import (
    ParcelReference,
    load_existing_org_context_tool,
    load_existing_parcel_context_tool,
)


OWNERSHIP_SWARM_MAX_ALIAS_ATTEMPTS = 4


@dataclass(frozen=True)
class MasterDecision:
    decision: str
    reason: str
    entity_result: dict | None
    review_reasons: list[str]
    explanation: str


class NullTracker:
    run_id = None

    def start(self, *args, **kwargs):
        return None

    def checkpoint(self, *args, **kwargs):
        return None

    def create_task(self, *args, **kwargs):
        return None

    def complete_task(self, *args, **kwargs):
        return None

    def fail_task(self, *args, **kwargs):
        return None

    def create_artifact(self, *args, **kwargs):
        return None

    def create_proposal(self, *args, **kwargs):
        return None

    def add_validation(self, *args, **kwargs):
        return None

    def ensure_review(self, *args, **kwargs):
        return None

    def mark_proposal_promoted(self, *args, **kwargs):
        return None


def _run_task(tracker, task_type, input_payload, fn, parent_task_id=None, skip=False):
    task_id = tracker.create_task(task_type, input_payload=input_payload, parent_task_id=parent_task_id)
    if skip:
        tracker.complete_task(task_id, {"skipped": True}, status=TASK_STATUS_SKIPPED)
        return task_id, {"skipped": True}
    try:
        output_payload = fn()
    except Exception as exc:
        tracker.fail_task(task_id, str(exc), {"error": str(exc)})
        raise
    tracker.complete_task(task_id, output_payload)
    return task_id, output_payload


def generate_owner_aliases(owner_name, grouped_records):
    aliases = []

    def add(name):
        normalized = " ".join((name or "").split())
        if not normalized:
            return
        if normalized not in aliases:
            aliases.append(normalized)

    add(owner_name)
    normalized_owner = normalize_org_name(owner_name)
    if normalized_owner and normalized_owner != owner_name.lower():
        add(normalized_owner.title())

    compact = re.sub(r"\b(owner|holdings|holding|urban renewal|redevelopment|property|properties)\b", "", owner_name or "", flags=re.I)
    compact = " ".join(compact.replace(",", " ").split())
    add(compact)

    streetless = re.sub(r"\b(street|st|avenue|ave|road|rd|boulevard|blvd)\b", "", compact, flags=re.I)
    streetless = " ".join(streetless.split())
    add(streetless)

    for record in grouped_records or []:
        for party in (record.get("grantees") or []) + (record.get("grantors") or []):
            if normalize_org_name(party) == normalized_owner:
                continue
            add(party)

    return aliases[:OWNERSHIP_SWARM_MAX_ALIAS_ATTEMPTS]


def _select_best_registry_result(search_runs, canonical_owner_name=None):
    candidates = []
    for item in search_runs:
        for candidate in item["match"]["candidates"]:
            enriched = {
                **candidate,
                "searched_name": item["searched_name"],
                "details": item["details"],
            }
            if not any(existing["entity_id"] == enriched["entity_id"] for existing in candidates if existing.get("entity_id")):
                candidates.append(enriched)

    matched = [item for item in search_runs if item["match"]["status"] == "matched" and item["match"]["match"]]
    unique_matched = {}
    for item in matched:
        entity = item["match"]["match"]
        key = entity["entity_id"] or entity["business_name"]
        existing = unique_matched.get(key)
        if not existing or entity["match_score"] > existing["match"]["match"]["match_score"]:
            unique_matched[key] = item

    canonical_owner_normalized = normalize_org_name(canonical_owner_name or "")
    canonical_owner_key = " ".join((canonical_owner_name or "").lower().split())
    canonical_review_matches = []
    for item in search_runs:
        searched_name_key = " ".join((item["searched_name"] or "").lower().split())
        if searched_name_key != canonical_owner_key:
            continue
        if item["match"]["status"] == "needs_review":
            canonical_review_matches.extend(item["match"]["candidates"])
    if canonical_review_matches:
        deduped = []
        seen = set()
        for candidate in sorted(canonical_review_matches, key=lambda row: (-row["match_score"], row["business_name"])):
            key = candidate["entity_id"]
            if key in seen:
                continue
            seen.add(key)
            deduped.append(candidate)
        return {
            "source_name": "nj_business_name_search",
            "source_url": search_runs[0]["source_url"],
            "access_date": search_runs[0]["access_date"],
            "owner_name": canonical_owner_name,
            "match_status": "needs_review",
            "matched_entity": None,
            "candidates": deduped[:5],
            "details": {},
        }

    canonical_matches = [
        item
        for item in unique_matched.values()
        if " ".join((item["searched_name"] or "").lower().split()) == canonical_owner_key
        and normalize_org_name(item["match"]["match"]["business_name"]) == canonical_owner_normalized
    ]

    if len(canonical_matches) == 1:
        selected = canonical_matches[0]
        return {
            "source_name": "nj_business_name_search",
            "source_url": selected["source_url"],
            "access_date": selected["access_date"],
            "owner_name": selected["searched_name"],
            "match_status": "matched",
            "matched_entity": selected["match"]["match"],
            "candidates": selected["match"]["candidates"][:5],
            "details": selected["details"],
        }

    if len(unique_matched) == 1:
        selected = next(iter(unique_matched.values()))
        return {
            "source_name": "nj_business_name_search",
            "source_url": selected["source_url"],
            "access_date": selected["access_date"],
            "owner_name": selected["searched_name"],
            "match_status": "matched",
            "matched_entity": selected["match"]["match"],
            "candidates": selected["match"]["candidates"][:5],
            "details": selected["details"],
        }
    if len(unique_matched) > 1:
        top_candidates = sorted(
            [item["match"]["match"] for item in unique_matched.values() if item["match"]["match"]],
            key=lambda row: (-row["match_score"], row["business_name"]),
        )
        return {
            "source_name": "nj_business_name_search",
            "source_url": matched[0]["source_url"],
            "access_date": matched[0]["access_date"],
            "owner_name": matched[0]["searched_name"],
            "match_status": "needs_review",
            "matched_entity": None,
            "candidates": top_candidates[:5],
            "details": {},
        }

    review_matches = []
    for item in search_runs:
        if item["match"]["status"] == "needs_review":
            review_matches.extend(item["match"]["candidates"])
    if review_matches:
        deduped = []
        seen = set()
        for candidate in sorted(review_matches, key=lambda row: (-row["match_score"], row["business_name"])):
            key = candidate["entity_id"]
            if key in seen:
                continue
            seen.add(key)
            deduped.append(candidate)
        return {
            "source_name": "nj_business_name_search",
            "source_url": search_runs[0]["source_url"],
            "access_date": search_runs[0]["access_date"],
            "owner_name": search_runs[0]["searched_name"],
            "match_status": "needs_review",
            "matched_entity": None,
            "candidates": deduped[:5],
            "details": {},
        }

    return {
        "source_name": "nj_business_name_search",
        "source_url": search_runs[0]["source_url"] if search_runs else None,
        "access_date": search_runs[0]["access_date"] if search_runs else None,
        "owner_name": search_runs[0]["searched_name"] if search_runs else None,
        "match_status": "not_found",
        "matched_entity": None,
        "candidates": candidates[:5],
        "details": {},
    }


def adjudicate_ownership_swarm(parcel, recorder_result, entity_result, party_search_result, existing_orgs):
    latest_deed = recorder_result.get("latest_deed")
    if not latest_deed:
        return MasterDecision(
            decision="partial",
            reason="no_deed_found",
            entity_result=None,
            review_reasons=[],
            explanation="Recorder history was searched but no deed could be selected as the current owner-of-record.",
        )

    if entity_result["match_status"] == "matched":
        return MasterDecision(
            decision="accept",
            reason="single_clear_registry_match",
            entity_result=entity_result,
            review_reasons=[],
            explanation="Recorder deed owner resolved to a single NJ business registry candidate with no competing strong match.",
        )

    if entity_result["match_status"] == "needs_review":
        return MasterDecision(
            decision="needs_review",
            reason="ambiguous_registry_candidates",
            entity_result=entity_result,
            review_reasons=["multiple plausible NJ business entity candidates require analyst review"],
            explanation="The registry search produced multiple plausible entity candidates across alias attempts and cannot be promoted automatically.",
        )

    if existing_orgs and existing_orgs[0].get("nj_entity_id"):
        selected = {
            "source_name": "existing_organization_context",
            "source_url": None,
            "access_date": None,
            "owner_name": latest_deed["primary_grantee"],
            "match_status": "matched",
            "matched_entity": {
                "business_name": existing_orgs[0]["name"],
                "entity_id": existing_orgs[0]["nj_entity_id"],
                "match_score": 1.0,
            },
            "candidates": [],
            "details": {
                "status": existing_orgs[0]["status"],
                "registered_agent": existing_orgs[0]["registered_agent"],
                "officers": [],
            },
        }
        return MasterDecision(
            decision="accept",
            reason="reused_existing_org_context",
            entity_result=selected,
            review_reasons=[],
            explanation="No fresh NJ business search match was found, but an exact existing organization context already exists for this owner name.",
        )

    party_records = party_search_result.get("grouped_records") or []
    if party_records:
        return MasterDecision(
            decision="partial",
            reason="party_search_support_without_registry_match",
            entity_result=None,
            review_reasons=[],
            explanation="Recorder party-name evidence found related filings, but the owner could not be matched to a single NJ business entity.",
        )

    return MasterDecision(
        decision="partial",
        reason="deed_without_registry_match",
        entity_result=None,
        review_reasons=[],
        explanation="The latest deed is available and can be persisted, but NJ business matching stopped at the owner-of-record name.",
    )


def run_ownership_swarm(
    connection,
    market_id,
    parcel_id,
    recorder_tool,
    business_tool,
    tracker,
    force_refresh=False,
):
    tracker = tracker or NullTracker()
    tracker.start(
        "swarm_started",
        {
            "market_id": market_id,
            "parcel_id": parcel_id,
            "workflow_type": WORKFLOW_OWNERSHIP_V1,
        },
        "Ownership swarm started.",
    )

    load_task_id, parcel = _run_task(
        tracker,
        "load_existing_parcel_context",
        {"market_id": market_id, "parcel_id": parcel_id},
        lambda: load_existing_parcel_context_tool(connection, market_id, parcel_id),
    )
    if not parcel:
        raise ValueError("Parcel %s was not found." % parcel_id)
    tracker.create_artifact("parcel_context", parcel, produced_by_task_id=load_task_id)
    tracker.checkpoint(
        "load_existing_parcel_context",
        RUN_STATUS_IN_PROGRESS,
        "Loaded parcel context for ownership swarm.",
        {"parcel_id": parcel_id, "parcel_pin": parcel["parcel_pin"]},
        current_step="load_existing_parcel_context",
    )
    tracker.checkpoint(
        "load_parcel",
        RUN_STATUS_IN_PROGRESS,
        "Loaded parcel context.",
        {"parcel_id": parcel_id, "parcel_pin": parcel["parcel_pin"]},
        current_step="load_existing_parcel_context",
    )

    recorder_task_id, recorder_payload = _run_task(
        tracker,
        "recorder_analyst",
        {"block": parcel["block"], "lot": parcel["lot"], "municipality": "NEWARK"},
        lambda: recorder_tool.fetch_recorder_by_block_lot(
            ParcelReference(
                parcel_id=parcel["id"],
                municipality="NEWARK",
                block=parcel["block"],
                lot=parcel["lot"],
            ),
            force_refresh=force_refresh,
        ).to_state(),
    )
    tracker.create_artifact("recorder_result", recorder_payload, produced_by_task_id=recorder_task_id)
    tracker.checkpoint(
        "recorder_analyst",
        RUN_STATUS_IN_PROGRESS,
        "Recorder analyst gathered deed history and financing filings.",
        {
            "parcel_id": parcel_id,
            "document_count": len(recorder_payload.get("grouped_records") or []),
            "has_latest_deed": bool(recorder_payload.get("latest_deed")),
        },
        current_step="recorder_analyst",
    )
    tracker.checkpoint(
        "fetch_recorder",
        RUN_STATUS_IN_PROGRESS,
        "Fetched recorder data.",
        {
            "parcel_id": parcel_id,
            "document_count": len(recorder_payload.get("grouped_records") or []),
            "has_latest_deed": bool(recorder_payload.get("latest_deed")),
        },
        current_step="recorder_analyst",
    )

    latest_deed = recorder_payload.get("latest_deed") or {}
    owner_name = latest_deed.get("primary_grantee") or "; ".join(latest_deed.get("grantees") or []) or ""
    alias_task_id, alias_payload = _run_task(
        tracker,
        "alias_agent",
        {"owner_name": owner_name},
        lambda: {
            "owner_name": owner_name,
            "aliases": generate_owner_aliases(owner_name, recorder_payload.get("grouped_records") or []),
        },
        parent_task_id=recorder_task_id,
        skip=not owner_name,
    )
    tracker.create_artifact("alias_candidates", alias_payload, produced_by_task_id=alias_task_id)
    tracker.checkpoint(
        "alias_agent",
        RUN_STATUS_IN_PROGRESS,
        "Alias agent generated conservative entity-name variants.",
        {"owner_name": owner_name, "alias_count": len(alias_payload.get("aliases") or [])},
        current_step="alias_agent",
    )

    def _registry_runner():
        search_runs = []
        aliases = alias_payload.get("aliases") or [owner_name]
        for alias in aliases[:OWNERSHIP_SWARM_MAX_ALIAS_ATTEMPTS]:
            search_result = business_tool.search_njbgs_by_name(alias, force_refresh=force_refresh)
            details = business_tool.get_njbgs_entity_details(alias, force_refresh=force_refresh)
            match = choose_best_njbgs_match(alias, search_result.candidates)
            search_runs.append(
                {
                    **search_result.to_state(),
                    "details": details,
                    "match": match,
                }
            )
        return {
            "searches": search_runs,
            "selected": _select_best_registry_result(search_runs, canonical_owner_name=owner_name),
        }

    registry_task_id, registry_payload = _run_task(
        tracker,
        "registry_matcher",
        {"aliases": alias_payload.get("aliases") or []},
        _registry_runner,
        parent_task_id=alias_task_id,
        skip=not owner_name,
    )
    tracker.create_artifact("registry_searches", registry_payload, produced_by_task_id=registry_task_id)
    tracker.checkpoint(
        "registry_matcher",
        RUN_STATUS_IN_PROGRESS,
        "Registry matcher searched NJ business candidates across alias attempts.",
        {
            "match_status": (registry_payload.get("selected") or {}).get("match_status"),
            "search_count": len(registry_payload.get("searches") or []),
        },
        current_step="registry_matcher",
    )
    tracker.checkpoint(
        "fetch_business",
        RUN_STATUS_IN_PROGRESS,
        "Fetched NJ business registry match candidates.",
        {
            "match_status": (registry_payload.get("selected") or {}).get("match_status"),
            "search_count": len(registry_payload.get("searches") or []),
        },
        current_step="registry_matcher",
    )

    def _party_search_runner():
        searches = []
        discovered_names = []
        for alias in alias_payload.get("aliases") or [owner_name]:
            party_result = recorder_tool.search_recorder_by_party_name(alias, force_refresh=force_refresh).to_state()
            searches.append(party_result)
            for record in party_result.get("grouped_records") or []:
                for party in (record.get("grantors") or []) + (record.get("grantees") or []):
                    if party and party not in discovered_names:
                        discovered_names.append(party)
        return {
            "searches": searches,
            "grouped_records": searches[0]["grouped_records"] if searches else [],
            "discovered_names": discovered_names[:OWNERSHIP_SWARM_MAX_ALIAS_ATTEMPTS],
        }

    party_task_id, party_payload = _run_task(
        tracker,
        "party_search_agent",
        {"aliases": alias_payload.get("aliases") or []},
        _party_search_runner,
        parent_task_id=registry_task_id,
        skip=not owner_name,
    )
    tracker.create_artifact("party_search_support", party_payload, produced_by_task_id=party_task_id)
    tracker.checkpoint(
        "party_search_agent",
        RUN_STATUS_IN_PROGRESS,
        "Party search agent gathered recorder-side support signals.",
        {"discovered_name_count": len(party_payload.get("discovered_names") or [])},
        current_step="party_search_agent",
    )

    existing_org_task_id, existing_orgs = _run_task(
        tracker,
        "load_existing_org_context",
        {"owner_name": owner_name},
        lambda: load_existing_org_context_tool(connection, market_id, owner_name),
        parent_task_id=registry_task_id,
        skip=not owner_name,
    )
    tracker.create_artifact("existing_org_context", existing_orgs, produced_by_task_id=existing_org_task_id)

    evidence_task_id, evidence_payload = _run_task(
        tracker,
        "evidence_critic",
        {
            "owner_name": owner_name,
            "registry_match_status": (registry_payload.get("selected") or {}).get("match_status"),
        },
        lambda: {
            "owner_name": owner_name,
            "has_latest_deed": bool(latest_deed),
            "registry_match_status": (registry_payload.get("selected") or {}).get("match_status", "not_found"),
            "candidate_count": len((registry_payload.get("selected") or {}).get("candidates") or []),
            "financing_claim_count": len(extract_financing_claims(recorder_payload.get("grouped_records") or [])),
            "party_search_name_count": len(party_payload.get("discovered_names") or []),
            "existing_org_count": len(existing_orgs or []),
        },
        parent_task_id=party_task_id,
    )
    tracker.create_artifact("evidence_summary", evidence_payload, produced_by_task_id=evidence_task_id)

    def _master_runner():
        decision = adjudicate_ownership_swarm(
            parcel,
            recorder_payload,
            (registry_payload.get("selected") or {"match_status": "not_found"}),
            party_payload,
            existing_orgs,
        )
        return {
            "decision": decision.decision,
            "reason": decision.reason,
            "review_reasons": decision.review_reasons,
            "explanation": decision.explanation,
            "entity_result": decision.entity_result,
        }

    master_task_id, master_payload = _run_task(
        tracker,
        "master_adjudicator",
        {"parcel_id": parcel_id},
        _master_runner,
        parent_task_id=evidence_task_id,
    )
    tracker.create_artifact("master_explanation", master_payload, produced_by_task_id=master_task_id)

    evidence_refs = []
    for artifact_type in (
        "parcel_context",
        "recorder_result",
        "alias_candidates",
        "registry_searches",
        "party_search_support",
        "evidence_summary",
        "master_explanation",
    ):
        evidence_refs.append({"artifact_type": artifact_type})

    master_decision_proposal_id = tracker.create_proposal(
        "master_decision",
        master_payload,
        produced_by_task_id=master_task_id,
        decision=master_payload["decision"],
        evidence_refs=evidence_refs,
        review_state=(
            REVIEW_STATUS_PENDING
            if master_payload["decision"] in {"needs_review", "conflict"}
            else "not_required"
        ),
        status=(
            PROPOSAL_STATUS_NEEDS_REVIEW
            if master_payload["decision"] in {"needs_review", "conflict"}
            else "proposed"
        ),
    )
    bundle_payload = {
        "market_id": market_id,
        "parcel_id": parcel_id,
        "decision": master_payload["decision"],
        "reason": master_payload["reason"],
        "confidence_tier": CONFIDENCE_PROBABLE,
        "recorder_result": recorder_payload,
        "entity_result": master_payload["entity_result"],
        "owner_name": owner_name,
        "explanation": master_payload["explanation"],
        "review_reasons": master_payload["review_reasons"],
    }
    bundle_proposal_id = tracker.create_proposal(
        "ownership_promotion_bundle",
        bundle_payload,
        produced_by_task_id=master_task_id,
        decision=master_payload["decision"],
        evidence_refs=evidence_refs,
        review_state=(
            REVIEW_STATUS_PENDING
            if master_payload["decision"] in {"needs_review", "conflict"}
            else "not_required"
        ),
        status=(
            PROPOSAL_STATUS_NEEDS_REVIEW
            if master_payload["decision"] in {"needs_review", "conflict"}
            else "proposed"
        ),
    )
    tracker.create_proposal(
        "ownership_claim",
        {
            "parcel_id": parcel_id,
            "owner_name": owner_name,
            "latest_deed": latest_deed,
            "decision": master_payload["decision"],
        },
        produced_by_task_id=master_task_id,
        decision=master_payload["decision"],
        evidence_refs=evidence_refs,
    )
    tracker.create_proposal(
        "financing_claims",
        {
            "parcel_id": parcel_id,
            "financing_claims": extract_financing_claims(recorder_payload.get("grouped_records") or []),
        },
        produced_by_task_id=master_task_id,
        decision="accept",
        evidence_refs=evidence_refs,
    )

    for proposal_id, proposal_type, payload in (
        (master_decision_proposal_id, "master_decision", master_payload),
        (bundle_proposal_id, "ownership_promotion_bundle", bundle_payload),
    ):
        validation = validate_proposal(proposal_type, payload)
        tracker.add_validation(
            proposal_id,
            "deterministic_policy_gateway",
            validation["outcome"],
            validation["message"],
            validation["payload"],
        )
        if validation["outcome"] == VALIDATION_OUTCOME_NEEDS_REVIEW:
            tracker.ensure_review(proposal_id, notes=validation["message"])

    final_status = "completed"
    promoted_result = None
    bundle_validation = validate_proposal("ownership_promotion_bundle", bundle_payload)
    if should_auto_promote(bundle_validation["outcome"], bundle_payload):
        promoted_result = promote_proposal(
            connection,
            {
                "proposal_type": "ownership_promotion_bundle",
                "payload": bundle_payload,
            },
        )
        tracker.mark_proposal_promoted(bundle_proposal_id)
    elif bundle_validation["outcome"] == VALIDATION_OUTCOME_NEEDS_REVIEW:
        final_status = STATUS_NEEDS_REVIEW
        mark_parcel_review_pending(
            connection,
            market_id,
            parcel_id,
            "Ownership swarm requires analyst review before promotion.",
            metadata={
                "owner_name": owner_name,
                "latest_deed": latest_deed,
                "review_reasons": master_payload["review_reasons"] or [bundle_validation["message"]],
                "search_candidates": ((registry_payload.get("selected") or {}).get("candidates") or []),
                "agentic_decision": master_payload["decision"],
                "latest_agent_run_id": tracker.run_id,
            },
        )
    elif bundle_validation["outcome"] == VALIDATION_OUTCOME_FAILED:
        final_status = STATUS_FAILED
        mark_parcel_failed(connection, market_id, parcel_id, bundle_validation["message"])

    tracker.checkpoint(
        "promotion_gateway",
        RUN_STATUS_IN_PROGRESS,
        "Promotion gateway evaluated the ownership swarm outputs.",
        {
            "decision": master_payload["decision"],
            "validation_outcome": bundle_validation["outcome"],
            "auto_promoted": bool(promoted_result),
        },
        current_step="promotion_gateway",
    )

    return {
        "market_id": market_id,
        "parcel_id": parcel_id,
        "workflow_type": WORKFLOW_OWNERSHIP_V1,
        "enrichment_status": final_status,
        "decision": master_payload["decision"],
        "reason": master_payload["reason"],
        "review_reasons": master_payload["review_reasons"],
        "auto_promoted": bool(promoted_result),
        "promoted_result": promoted_result,
        "persistence_result": promoted_result
        or {
            "parcel_id": parcel_id,
            "enrichment_status": final_status,
            "review_reasons": master_payload["review_reasons"],
            "qa": {},
        },
        "run_status": final_status,
        "selected_entity_match_status": (master_payload["entity_result"] or {}).get("match_status"),
    }


register_swarm(
    SwarmDefinition(
        workflow_type=WORKFLOW_OWNERSHIP_V1,
        display_name="Newark Ownership Swarm V1",
        roles=(
            "recorder_analyst",
            "alias_agent",
            "registry_matcher",
            "party_search_agent",
            "evidence_critic",
            "master_adjudicator",
        ),
        tool_whitelist=(
            "fetch_recorder_by_block_lot",
            "search_recorder_by_party_name",
            "search_njbgs_by_name",
            "get_njbgs_entity_details",
            "load_existing_parcel_context",
            "load_existing_org_context",
            "write_structured_proposal",
        ),
        max_depth=5,
        max_worker_fanout=6,
        max_retries=3,
        max_tool_calls=12,
        timeout_seconds=300,
        handler=run_ownership_swarm,
    )
)
