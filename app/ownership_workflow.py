from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, TypedDict

from app.ownership_service import load_parcel_context, persist_ownership_result
from app.ownership_swarm import run_ownership_swarm


class OwnershipWorkflowState(TypedDict, total=False):
    market_id: str
    parcel_id: str
    parcel: dict
    recorder_result: dict
    entity_result: dict
    persistence_result: dict
    status: str
    error: Optional[str]


@dataclass
class OwnershipWorkflowContext:
    connection: object
    tracker: Optional[object]
    recorder_tool: object
    business_tool: object
    force_refresh: bool = False

    def checkpoint(self, step_name, message, state):
        if not self.tracker:
            return
        self.tracker.checkpoint(
            step_name,
            "in_progress",
            message,
            state_payload=state,
            current_step=step_name,
        )


class _LegacyOwnershipWorkflow:
    def __init__(self, context: OwnershipWorkflowContext):
        self.context = context

    def invoke(self, initial_state: OwnershipWorkflowState):
        state = dict(initial_state)
        parcel = load_parcel_context(self.context.connection, state["market_id"], state["parcel_id"])
        if not parcel:
            raise ValueError("Parcel not found for ownership workflow.")
        state["parcel"] = parcel
        self.context.checkpoint("load_parcel", "Loaded parcel context.", state)

        recorder_result = self.context.recorder_tool.fetch_latest_deed(
            type(
                "ParcelRef",
                (),
                {
                    "parcel_id": parcel["id"],
                    "municipality": "NEWARK",
                    "block": parcel["block"],
                    "lot": parcel["lot"],
                },
            )(),
            force_refresh=self.context.force_refresh,
        ).to_state()
        state["recorder_result"] = recorder_result
        self.context.checkpoint("fetch_recorder", "Fetched recorder data.", state)

        entity_result = None
        latest_deed = recorder_result.get("latest_deed")
        if latest_deed:
            owner_name = latest_deed.get("primary_grantee") or "; ".join(latest_deed.get("grantees") or []) or "Unknown owner"
            entity_result = self.context.business_tool.match_owner(
                owner_name,
                force_refresh=self.context.force_refresh,
            ).to_state()
            state["entity_result"] = entity_result
            self.context.checkpoint(
                "fetch_business",
                "Fetched NJ business registry match candidates.",
                state,
            )

        persistence_result = persist_ownership_result(
            self.context.connection,
            state["market_id"],
            state["parcel_id"],
            recorder_result,
            entity_result,
        )
        state["persistence_result"] = persistence_result
        state["status"] = persistence_result["enrichment_status"]
        self.context.checkpoint("persist_result", "Persisted deterministic ownership result.", state)
        return state


def build_ownership_workflow(context: OwnershipWorkflowContext):
    return _LegacyOwnershipWorkflow(context)


def run_ownership_workflow(
    connection,
    market_id,
    parcel_id,
    recorder_tool,
    business_tool,
    tracker=None,
    force_refresh=False,
):
    return run_ownership_swarm(
        connection=connection,
        market_id=market_id,
        parcel_id=parcel_id,
        recorder_tool=recorder_tool,
        business_tool=business_tool,
        tracker=tracker,
        force_refresh=force_refresh,
    )
