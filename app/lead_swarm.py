from __future__ import annotations

from app.agent_registry import SwarmDefinition, register_swarm
from app.agent_runs import (
    PROPOSAL_STATUS_NEEDS_REVIEW,
    REVIEW_STATUS_PENDING,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_IN_PROGRESS,
    VALIDATION_OUTCOME_FAILED,
    VALIDATION_OUTCOME_NEEDS_REVIEW,
)
from app.agent_validation import promote_proposal, should_auto_promote, validate_proposal
from app.lead_scoring import PROMOTEABLE_PERSONAS
from app.lead_service import (
    build_developer_seed_candidates,
    build_source_document,
    synthesize_lead_payload,
)


WORKFLOW_LEAD_SYNTHESIS_V1 = "newark_lead_synthesis_v1"
WORKFLOW_CONTACT_ENRICHMENT_V1 = "newark_contact_enrichment_v1"

LEAD_PARSER_VERSION = "newark-lead-v1"


def _first_or_none(values):
    return values[0] if values else None


def run_lead_synthesis_swarm(
    connection,
    market_id,
    tracker,
    llm_provider,
    roster_adapter,
    force_refresh=False,
    source_definitions=None,
    top_n=75,
):
    tracker.start(
        "lead_swarm_started",
        {
            "market_id": market_id,
            "workflow_type": WORKFLOW_LEAD_SYNTHESIS_V1,
            "persona": "lead",
            "source_families": sorted({item.source_family for item in (source_definitions or [])} | {"ownership_officer"}),
            "promotion_state": "lead_generation",
        },
        "Lead synthesis swarm started.",
    )
    task_id = tracker.create_task(
        "source_analyst",
        {"market_id": market_id, "top_n": top_n},
    )
    developer_candidates = build_developer_seed_candidates(connection, market_id, limit=top_n)
    roster_results = []
    tracker.complete_task(
        task_id,
        {"developer_seed_count": len(developer_candidates), "source_count": len(source_definitions or [])},
    )

    for source in source_definitions or []:
        source_task_id = tracker.create_task(
            "roster_extractor",
            {"source_id": source.id, "source_family": source.source_family, "url": source.url},
            parent_task_id=task_id,
        )
        result = roster_adapter.fetch_people(source, force_refresh=force_refresh)
        roster_results.append(result)
        tracker.create_artifact(
            "lead_source_result",
            result.to_state(),
            produced_by_task_id=source_task_id,
        )
        tracker.complete_task(
            source_task_id,
            {"people_count": len(result.people), "contacts_found": len(result.contacts)},
        )

    candidates = developer_candidates[:]
    for result in roster_results:
        source_document_id = "source-%s" % result.source.id
        for item in result.people:
            contacts = []
            if item.get("email"):
                contacts.append(
                    {
                        "contact_type": "email",
                        "contact_value": item["email"],
                        "label": item.get("organization_name"),
                        "source_family": result.source.source_family,
                        "source_document_id": source_document_id,
                    }
                )
            if item.get("phone"):
                contacts.append(
                    {
                        "contact_type": "phone",
                        "contact_value": item["phone"],
                        "label": item.get("organization_name"),
                        "source_family": result.source.source_family,
                        "source_document_id": source_document_id,
                    }
                )
            if item.get("office_address"):
                contacts.append(
                    {
                        "contact_type": "office_address",
                        "contact_value": item["office_address"],
                        "label": item.get("organization_name"),
                        "source_family": result.source.source_family,
                        "source_document_id": source_document_id,
                    }
                )
            contacts.append(
                {
                    "contact_type": "website_profile",
                    "contact_value": item.get("profile_url") or result.source_url,
                    "label": item.get("organization_name") or result.source.organization_name or result.source.title,
                    "source_family": result.source.source_family,
                    "source_document_id": source_document_id,
                }
            )
            candidates.append(
                {
                    "seed_type": result.source.source_family,
                    "source_family": result.source.source_family,
                    "persona_hint": result.source.persona_hint,
                    "full_name": item["full_name"],
                    "title": item["title"],
                    "organization_name": item.get("organization_name") or result.source.organization_name or result.source.title,
                    "parcel_ids": [],
                    "priority_scores": [],
                    "source_document_ids": [source_document_id],
                    "contacts": contacts,
                    "evidence_links": [
                        {
                            "evidence_type": result.source.source_family,
                            "source_document_id": source_document_id,
                            "notes": result.source.title,
                        }
                    ],
                    "evidence_types": [result.source.source_family],
                    "has_market_link": True,
                    "source_label": result.source.title,
                }
            )

    tracker.checkpoint(
        "candidate_selection",
        RUN_STATUS_IN_PROGRESS,
        "Lead source candidates assembled.",
        {"candidate_count": len(candidates)},
        current_step="candidate_selection",
    )

    promoted_results = []
    review_count = 0
    for index, candidate in enumerate(candidates):
        if not candidate.get("full_name"):
            continue
        source_task_id = tracker.create_task(
            "contact_finder",
            {"candidate_index": index, "full_name": candidate["full_name"]},
        )
        tracker.complete_task(
            source_task_id,
            {"contact_count": len(candidate.get("contacts", []))},
        )

        synthesis_task_id = tracker.create_task(
            "master_adjudicator",
            {"candidate_index": index, "full_name": candidate["full_name"]},
        )
        source_documents = []
        primary_source_document_id = (candidate.get("source_document_ids") or [None])[0]
        for result in roster_results:
            if "source-%s" % result.source.id in candidate.get("source_document_ids", []):
                source_documents.append(
                    build_source_document(
                        "source-%s" % result.source.id,
                        result.source_name,
                        result.source_document_title,
                        result.source_url,
                        result.access_date,
                        "public_html_profile",
                        LEAD_PARSER_VERSION,
                    )
                )
        if source_documents:
            primary_source_document_id = source_documents[0]["id"]

        lead_summary = synthesize_lead_payload(
            market_id=market_id,
            candidate={
                **candidate,
                "evidence_types": candidate.get("evidence_types") or [candidate.get("source_family")],
                "has_market_link": candidate.get("has_market_link", bool(candidate.get("parcel_ids")) or candidate.get("source_family") == "civic_roster"),
                "evidence_links": candidate.get("evidence_links")
                or [
                    {
                        "evidence_type": "parcel_ownership"
                        if candidate.get("parcel_ids")
                        else candidate.get("source_family"),
                        "parcel_id": _first_or_none(candidate.get("parcel_ids") or []),
                        "organization_id": candidate.get("organization_id"),
                        "source_document_id": _first_or_none(candidate.get("source_document_ids") or []),
                        "notes": candidate.get("title"),
                    }
                ],
            },
            source_documents=source_documents,
            llm_provider=llm_provider,
        )
        persona = lead_summary["persona"]
        if persona not in PROMOTEABLE_PERSONAS:
            tracker.complete_task(
                synthesis_task_id,
                {"skipped": True, "persona": persona},
            )
            continue
        proposal_payload = {
            "market_id": market_id,
            "persona": persona,
            "lead_score": lead_summary["lead_score"],
            "role_relevance_score": lead_summary["role_relevance_score"],
            "market_relevance_score": lead_summary["market_relevance_score"],
            "contactability_score": lead_summary["contactability_score"],
            "evidence_score": lead_summary["evidence_score"],
            "lead_status": lead_summary["lead_status"],
            "review_decision": lead_summary["review_decision"],
            "why_person_matters": lead_summary["why_person_matters"],
            "primary_source_document_id": primary_source_document_id,
            "source_documents": source_documents,
            "person": {
                "person_id": candidate.get("person_id"),
                "full_name": candidate["full_name"],
            },
            "organization": (
                {
                    "organization_id": candidate.get("organization_id"),
                    "name": candidate.get("organization_name"),
                }
                if candidate.get("organization_name") or candidate.get("organization_id")
                else None
            ),
            "affiliations": [
                {
                    "organization_id": candidate.get("organization_id"),
                    "title": candidate.get("title") or persona.title(),
                    "affiliation_type": candidate.get("seed_type"),
                    "source_document_id": primary_source_document_id,
                }
            ],
            "contacts": candidate.get("contacts", []),
            "evidence_links": candidate.get("evidence_links")
            or [
                {
                    "evidence_type": "parcel_ownership" if candidate.get("parcel_ids") else candidate.get("source_family"),
                    "parcel_id": _first_or_none(candidate.get("parcel_ids") or []),
                    "organization_id": candidate.get("organization_id"),
                    "source_document_id": primary_source_document_id,
                    "notes": candidate.get("title"),
                }
            ],
        }
        tracker.complete_task(
            synthesis_task_id,
            {
                "persona": persona,
                "lead_score": lead_summary["lead_score"],
                "decision": lead_summary["review_decision"],
            },
        )

        tracker.create_proposal(
            "person_proposal",
            {
                "full_name": candidate["full_name"],
                "title": candidate.get("title"),
                "organization_name": candidate.get("organization_name"),
                "persona": persona,
            },
            produced_by_task_id=synthesis_task_id,
        )
        tracker.create_proposal(
            "contact_proposal",
            {"contacts": candidate.get("contacts", []), "full_name": candidate["full_name"]},
            produced_by_task_id=synthesis_task_id,
        )
        review_state = (
            REVIEW_STATUS_PENDING
            if lead_summary["review_decision"] in {"needs_review", "conflict", "partial"}
            else "not_required"
        )
        proposal_status = (
            PROPOSAL_STATUS_NEEDS_REVIEW
            if review_state == REVIEW_STATUS_PENDING
            else "proposed"
        )
        proposal_id = tracker.create_proposal(
            "lead_proposal",
            proposal_payload,
            produced_by_task_id=synthesis_task_id,
            decision=lead_summary["review_decision"],
            evidence_refs=proposal_payload["evidence_links"],
            review_state=review_state,
            status=proposal_status,
        )
        validation = validate_proposal("lead_proposal", proposal_payload)
        tracker.add_validation(
            proposal_id,
            "lead_promotion_gateway",
            validation["outcome"],
            validation["message"],
            validation["payload"],
        )
        if validation["outcome"] == VALIDATION_OUTCOME_NEEDS_REVIEW or review_state == REVIEW_STATUS_PENDING:
            review_count += 1
            tracker.ensure_review(proposal_id, notes="Lead proposal requires review before promotion.")
            continue
        if validation["outcome"] == VALIDATION_OUTCOME_FAILED:
            continue
        if should_auto_promote(validation["outcome"], proposal_payload):
            promoted_result = promote_proposal(
                connection,
                {
                    "id": proposal_id,
                    "agent_run_id": tracker.run_id,
                    "proposal_type": "lead_proposal",
                    "payload": proposal_payload,
                },
            )
            tracker.mark_proposal_promoted(proposal_id)
            promoted_results.append(promoted_result)

    tracker.checkpoint(
        "promotion_gateway",
        RUN_STATUS_IN_PROGRESS,
        "Lead promotion gateway evaluated synthesized lead proposals.",
        {
            "promoted_count": len(promoted_results),
            "review_count": review_count,
        },
        current_step="promotion_gateway",
    )
    return {
        "market_id": market_id,
        "workflow_type": WORKFLOW_LEAD_SYNTHESIS_V1,
        "persona": "lead",
        "source_families": sorted({item.source_family for item in (source_definitions or [])} | {"ownership_officer"}),
        "promotion_state": "lead_generation",
        "promoted_count": len(promoted_results),
        "review_count": review_count,
        "lead_count": len(promoted_results) + review_count,
        "status": RUN_STATUS_COMPLETED,
    }


def run_contact_enrichment_swarm(connection, market_id, person_id, tracker, force_refresh=False):
    tracker.start(
        "contact_swarm_started",
        {
            "market_id": market_id,
            "person_id": person_id,
            "workflow_type": WORKFLOW_CONTACT_ENRICHMENT_V1,
            "persona": "person",
            "promotion_state": "contact_enrichment",
        },
        "Contact enrichment swarm started.",
    )
    tracker.checkpoint(
        "contact_swarm_completed",
        RUN_STATUS_COMPLETED,
        "Contact enrichment currently reuses already-promoted public contact points.",
        {"person_id": person_id, "force_refresh": force_refresh},
        current_step="completed",
    )
    return {
        "market_id": market_id,
        "person_id": person_id,
        "workflow_type": WORKFLOW_CONTACT_ENRICHMENT_V1,
        "persona": "person",
        "promotion_state": "contact_enrichment",
        "status": RUN_STATUS_COMPLETED,
    }


register_swarm(
    SwarmDefinition(
        workflow_type=WORKFLOW_LEAD_SYNTHESIS_V1,
        display_name="Newark Lead Synthesis Swarm V1",
        roles=(
            "source_analyst",
            "roster_extractor",
            "contact_finder",
            "evidence_critic",
            "master_adjudicator",
        ),
        tool_whitelist=(
            "load_existing_org_context",
            "load_existing_parcel_context",
            "fetch_public_html",
            "write_structured_proposal",
        ),
        max_depth=6,
        max_worker_fanout=20,
        max_retries=2,
        max_tool_calls=50,
        timeout_seconds=600,
        handler=run_lead_synthesis_swarm,
    )
)

register_swarm(
    SwarmDefinition(
        workflow_type=WORKFLOW_CONTACT_ENRICHMENT_V1,
        display_name="Newark Contact Enrichment Swarm V1",
        roles=("contact_finder", "evidence_critic", "master_adjudicator"),
        tool_whitelist=("load_existing_org_context", "fetch_public_html", "write_structured_proposal"),
        max_depth=3,
        max_worker_fanout=8,
        max_retries=2,
        max_tool_calls=20,
        timeout_seconds=180,
        handler=run_contact_enrichment_swarm,
    )
)
