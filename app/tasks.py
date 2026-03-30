from __future__ import annotations

from celery import Celery

from app.agent_registry import get_swarm_definition
from app.agent_runs import (
    AgentRunTracker,
    RUN_STATUS_CANCELLED,
    WORKFLOW_CONTACT_ENRICHMENT_V1,
    WORKFLOW_LEAD_SYNTHESIS_V1,
    WORKFLOW_OWNERSHIP_V1,
    get_agent_run,
)
from app.config import get_settings
from app.db import LOCK_NAMESPACE_OWNERSHIP, advisory_job_lock, ensure_schema, get_connection
from app.lead_sources import PublicHtmlClient, PublicRosterAdapter, load_lead_source_definitions
from app.lead_swarm import run_contact_enrichment_swarm, run_lead_synthesis_swarm  # noqa: F401 - registers the swarms
from app.llm_provider import build_llm_provider
from app.ownership import NJBusinessSearchClient, EssexRecorderClient
from app.ownership_service import mark_parcel_failed, mark_parcel_in_progress
from app.ownership_swarm import run_ownership_swarm  # noqa: F401 - registers the swarm
from app.ownership_tools import BusinessRegistryTool, RecorderTool


def _build_celery_app():
    settings = get_settings()
    app = Celery("market_intel")
    broker_url = settings.redis_url
    result_backend = settings.redis_url
    if settings.celery_task_always_eager:
        broker_url = "memory://"
        result_backend = "cache+memory://"
    app.conf.update(
        broker_url=broker_url,
        result_backend=result_backend,
        task_always_eager=settings.celery_task_always_eager,
        task_store_eager_result=settings.celery_task_always_eager,
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
    )
    return app


celery_app = _build_celery_app()


def _build_ownership_tools(settings):
    cache_dir = settings.raw_dir / "ownership"
    recorder_tool = RecorderTool(
        EssexRecorderClient(
            base_url=settings.essex_recorder_url,
            cache_dir=cache_dir,
            request_delay_seconds=settings.ownership_request_delay_seconds,
            fixtures_dir=settings.ownership_fixtures_dir,
        )
    )
    business_tool = BusinessRegistryTool(
        NJBusinessSearchClient(
            base_url=settings.nj_business_search_url,
            cache_dir=cache_dir,
            request_delay_seconds=settings.ownership_request_delay_seconds,
            fixtures_dir=settings.ownership_fixtures_dir,
        )
    )
    return recorder_tool, business_tool


def _build_lead_tools(settings):
    cache_dir = settings.raw_dir / "leads"
    provider = build_llm_provider(settings)
    html_client = PublicHtmlClient(
        cache_dir=cache_dir,
        request_delay_seconds=settings.ownership_request_delay_seconds,
        fixtures_dir=settings.lead_fixtures_dir,
    )
    roster_adapter = PublicRosterAdapter(html_client, provider)
    source_definitions = load_lead_source_definitions(settings)
    return provider, roster_adapter, source_definitions


def _dispatch_registered_swarm(connection, run, tracker):
    definition = get_swarm_definition(run["workflow_type"])
    if not definition:
        raise ValueError("Unsupported workflow type %s." % run["workflow_type"])
    settings = get_settings()
    if run["workflow_type"] == WORKFLOW_OWNERSHIP_V1:
        if run["entity_type"] != "parcel":
            raise ValueError("Unsupported entity type %s." % run["entity_type"])
        recorder_tool, business_tool = _build_ownership_tools(settings)
        return definition.handler(
            connection=connection,
            market_id=run["market_id"],
            parcel_id=run["entity_id"],
            recorder_tool=recorder_tool,
            business_tool=business_tool,
            tracker=tracker,
            force_refresh=False,
        )
    if run["workflow_type"] == WORKFLOW_LEAD_SYNTHESIS_V1:
        provider, roster_adapter, source_definitions = _build_lead_tools(settings)
        state_payload = run["state_payload"] or {}
        return definition.handler(
            connection=connection,
            market_id=run["market_id"],
            tracker=tracker,
            llm_provider=provider,
            roster_adapter=roster_adapter,
            source_definitions=source_definitions,
            force_refresh=bool(state_payload.get("force_refresh")),
            top_n=int(state_payload.get("top_n") or 75),
        )
    if run["workflow_type"] == WORKFLOW_CONTACT_ENRICHMENT_V1:
        state_payload = run["state_payload"] or {}
        return definition.handler(
            connection=connection,
            market_id=run["market_id"],
            person_id=run["entity_id"],
            tracker=tracker,
            force_refresh=bool(state_payload.get("force_refresh")),
        )
    raise ValueError("Unsupported workflow type %s." % run["workflow_type"])


@celery_app.task(name="app.tasks.run_agent_run")
def run_agent_run(agent_run_id):
    settings = get_settings()
    tracker = AgentRunTracker(settings.database_url, agent_run_id)
    connection = get_connection(settings.database_url)
    try:
        ensure_schema(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, market_id, entity_type, entity_id, workflow_type, status, state_payload
                FROM agent_runs
                WHERE id = %s
                """,
                (agent_run_id,),
            )
            run = cursor.fetchone()
        if not run:
            raise ValueError("Agent run %s was not found." % agent_run_id)
        if run["status"] == RUN_STATUS_CANCELLED:
            return {"status": RUN_STATUS_CANCELLED}

        if run["workflow_type"] == WORKFLOW_OWNERSHIP_V1:
            with advisory_job_lock(
                connection,
                LOCK_NAMESPACE_OWNERSHIP,
                run["market_id"],
                wait_timeout_seconds=900,
            ):
                parcel = mark_parcel_in_progress(
                    connection,
                    run["market_id"],
                    run["entity_id"],
                    note="Ownership swarm queued and running.",
                )
                if not parcel:
                    raise ValueError("Parcel %s was not found." % run["entity_id"])
                final_state = _dispatch_registered_swarm(connection, run, tracker)
                tracker.complete(final_state, "Swarm run completed.")
                return final_state

        final_state = _dispatch_registered_swarm(connection, run, tracker)
        tracker.complete(final_state, "Swarm run completed.")
        return final_state
    except Exception as exc:
        connection.rollback()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT market_id, entity_id, workflow_type FROM agent_runs WHERE id = %s",
                    (agent_run_id,),
                )
                run = cursor.fetchone()
            if run and run["workflow_type"] == WORKFLOW_OWNERSHIP_V1:
                mark_parcel_failed(connection, run["market_id"], run["entity_id"], str(exc))
        finally:
            tracker.fail(str(exc), "failed", {"error": str(exc)})
        raise
    finally:
        connection.close()


@celery_app.task(name="app.tasks.run_ownership_agent_run")
def run_ownership_agent_run(agent_run_id):
    run_payload = None
    connection = get_connection(get_settings().database_url)
    try:
        run_payload = get_agent_run(connection, agent_run_id)
    finally:
        connection.close()
    if run_payload and run_payload["workflow_type"] != WORKFLOW_OWNERSHIP_V1:
        raise ValueError("Ownership wrapper received unsupported workflow type %s." % run_payload["workflow_type"])
    return run_agent_run(agent_run_id)
