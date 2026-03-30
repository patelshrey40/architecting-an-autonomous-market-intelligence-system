from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.agent_runs import (
    WORKFLOW_CONTACT_ENRICHMENT_V1,
    WORKFLOW_LEAD_SYNTHESIS_V1,
    WORKFLOW_OWNERSHIP_V1,
    cancel_agent_run,
    enqueue_agent_run,
    get_agent_proposal,
    get_agent_run,
    get_agent_run_artifacts,
    get_agent_run_proposals,
    get_agent_run_tasks,
    list_agent_runs,
    mark_proposal_promoted,
    resolve_review,
    retry_agent_run,
)
from app.agent_validation import promote_proposal
from app.config import get_settings
from app.db import ensure_schema, get_connection
from app.repository import (
    get_buildings,
    get_lead_detail,
    get_leads,
    get_parcel_detail,
    get_parcel_ownership,
    get_parcels,
    get_redevelopment_areas,
    get_summary,
    get_transit_stops,
    parse_bbox,
)
from app.tasks import run_agent_run, run_ownership_agent_run


settings = get_settings()

app = FastAPI(
    title="Architecting an Autonomous Market Intelligence System",
    description="Newark real-data foundation app for parcel intelligence and map exploration.",
    version="0.4.0",
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


def _with_connection(handler, ensure=False):
    connection = get_connection(settings.database_url)
    try:
        if ensure:
            ensure_schema(connection)
        return handler(connection)
    finally:
        connection.close()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "page_title": "Newark Market Intelligence Foundation",
            "page_subtitle": "Real Newark parcels, recruiter-grade leads, and evidence-backed swarms on OpenStreetMap.",
        },
    )


@app.get("/ops/agents", response_class=HTMLResponse)
def agent_ops(request: Request):
    return templates.TemplateResponse(
        "ops_agents.html",
        {
            "request": request,
            "page_title": "Agent Operations",
            "page_subtitle": "Inspect swarm runs, proposals, validations, and review states.",
        },
    )


@app.get("/healthz")
def healthcheck():
    return {"status": "ok"}


@app.get("/api/newark/summary")
def newark_summary():
    summary = _with_connection(lambda connection: get_summary(connection, settings.market_id))
    if not summary:
        raise HTTPException(status_code=404, detail="Newark data has not been ingested yet")
    return summary


@app.get("/api/newark/parcels")
def newark_parcels(
    bbox: str = Query(default=None),
    classification: str = Query(default=None),
    tier: str = Query(default=None),
    q: str = Query(default=None),
    min_value: float = Query(default=None),
):
    try:
        parsed_bbox = parse_bbox(bbox)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return _with_connection(
        lambda connection: get_parcels(
            connection,
            settings.market_id,
            bbox=parsed_bbox,
            classification=classification,
            tier=tier,
            minimum_value=min_value,
            search_term=q,
        )
    )


@app.get("/api/newark/buildings")
def newark_buildings(bbox: str = Query(default=None)):
    try:
        parsed_bbox = parse_bbox(bbox)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return _with_connection(
        lambda connection: get_buildings(connection, settings.market_id, bbox=parsed_bbox)
    )


@app.get("/api/newark/transit-stops")
def newark_transit_stops(bbox: str = Query(default=None)):
    try:
        parsed_bbox = parse_bbox(bbox)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return _with_connection(
        lambda connection: get_transit_stops(connection, settings.market_id, bbox=parsed_bbox)
    )


@app.get("/api/newark/redevelopment-areas")
def newark_redevelopment_areas(bbox: str = Query(default=None)):
    try:
        parsed_bbox = parse_bbox(bbox)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return _with_connection(
        lambda connection: get_redevelopment_areas(
            connection,
            settings.market_id,
            bbox=parsed_bbox,
        )
    )


@app.get("/api/newark/parcels/{parcel_id}")
def newark_parcel_detail(parcel_id: str):
    detail = _with_connection(
        lambda connection: get_parcel_detail(connection, settings.market_id, parcel_id)
    )
    if not detail:
        raise HTTPException(status_code=404, detail="Parcel not found")
    return detail


@app.get("/api/newark/parcels/{parcel_id}/ownership")
def newark_parcel_ownership(parcel_id: str):
    ownership = _with_connection(
        lambda connection: get_parcel_ownership(connection, settings.market_id, parcel_id),
        ensure=True,
    )
    if not ownership:
        raise HTTPException(status_code=404, detail="Parcel not found")
    return ownership


@app.get("/api/newark/leads")
def newark_leads(
    persona: str = Query(default=None),
    score_min: float = Query(default=None),
    contactable: bool = Query(default=None),
    org_id: str = Query(default=None),
    parcel_id: str = Query(default=None),
    q: str = Query(default=None),
):
    return _with_connection(
        lambda connection: get_leads(
            connection,
            settings.market_id,
            persona=persona,
            score_min=score_min,
            contactable=contactable,
            org_id=org_id,
            parcel_id=parcel_id,
            search_term=q,
        ),
        ensure=True,
    )


@app.get("/api/newark/leads/{lead_id}")
def newark_lead_detail(lead_id: str):
    detail = _with_connection(
        lambda connection: get_lead_detail(connection, settings.market_id, lead_id),
        ensure=True,
    )
    if not detail:
        raise HTTPException(status_code=404, detail="Lead not found")
    return detail


@app.post("/api/newark/leads/enrich")
def enqueue_newark_leads(top_n: int = Query(default=75, ge=1, le=300)):
    queued = _with_connection(
        lambda connection: enqueue_agent_run(
            connection,
            settings.market_id,
            "market",
            settings.market_id,
            WORKFLOW_LEAD_SYNTHESIS_V1,
            {
                "market_id": settings.market_id,
                "top_n": top_n,
                "source_families": ["ownership_officer", "civic_roster"],
                "promotion_state": "lead_generation",
                "persona": "lead",
            },
        ),
        ensure=True,
    )
    if queued["created"]:
        run_agent_run.delay(queued["run_id"])
    run_payload = _with_connection(lambda connection: get_agent_run(connection, queued["run_id"]), ensure=True)
    return {"agent_run_id": queued["run_id"], "created": queued["created"], "run": run_payload}


@app.post("/api/newark/people/{person_id}/contact-enrich")
def enqueue_newark_person_contact_enrichment(person_id: str):
    queued = _with_connection(
        lambda connection: enqueue_agent_run(
            connection,
            settings.market_id,
            "person",
            person_id,
            WORKFLOW_CONTACT_ENRICHMENT_V1,
            {
                "market_id": settings.market_id,
                "person_id": person_id,
                "promotion_state": "contact_enrichment",
                "persona": "person",
            },
        ),
        ensure=True,
    )
    if queued["created"]:
        run_agent_run.delay(queued["run_id"])
    run_payload = _with_connection(lambda connection: get_agent_run(connection, queued["run_id"]), ensure=True)
    return {"agent_run_id": queued["run_id"], "created": queued["created"], "run": run_payload}


@app.post("/api/newark/parcels/{parcel_id}/ownership/enrich")
def enqueue_newark_parcel_ownership(parcel_id: str):
    def handler(connection):
        parcel = get_parcel_detail(connection, settings.market_id, parcel_id)
        if not parcel:
            return None
        return enqueue_agent_run(
            connection,
            settings.market_id,
            "parcel",
            parcel_id,
            WORKFLOW_OWNERSHIP_V1,
            {
                "market_id": settings.market_id,
                "parcel_id": parcel_id,
            },
        )

    queued = _with_connection(handler, ensure=True)
    if not queued:
        raise HTTPException(status_code=404, detail="Parcel not found")
    if queued["created"]:
        run_ownership_agent_run.delay(queued["run_id"])
    run_payload = _with_connection(lambda connection: get_agent_run(connection, queued["run_id"]))
    return {
        "agent_run_id": queued["run_id"],
        "created": queued["created"],
        "run": run_payload,
    }


@app.get("/api/agent-runs")
def list_agent_run_items(
    workflow_type: str = Query(default=None),
    status: str = Query(default=None),
    entity_id: str = Query(default=None),
    review_state: str = Query(default=None),
    swarm_type: str = Query(default=None),
    persona: str = Query(default=None),
    promotion_state: str = Query(default=None),
    source_family: str = Query(default=None),
):
    return _with_connection(
        lambda connection: list_agent_runs(
            connection,
            workflow_type=swarm_type or workflow_type,
            status=status,
            entity_id=entity_id,
            review_state=review_state,
            persona=persona,
            promotion_state=promotion_state,
            source_family=source_family,
        ),
        ensure=True,
    )


@app.get("/api/agent-runs/{agent_run_id}")
def get_agent_run_status(agent_run_id: str):
    run = _with_connection(lambda connection: get_agent_run(connection, agent_run_id), ensure=True)
    if not run:
        raise HTTPException(status_code=404, detail="Agent run not found")
    return run


@app.get("/api/agent-runs/{agent_run_id}/tasks")
def get_agent_run_task_items(agent_run_id: str):
    run = _with_connection(lambda connection: get_agent_run(connection, agent_run_id), ensure=True)
    if not run:
        raise HTTPException(status_code=404, detail="Agent run not found")
    return _with_connection(lambda connection: get_agent_run_tasks(connection, agent_run_id), ensure=True)


@app.get("/api/agent-runs/{agent_run_id}/artifacts")
def get_agent_run_artifact_items(agent_run_id: str):
    run = _with_connection(lambda connection: get_agent_run(connection, agent_run_id), ensure=True)
    if not run:
        raise HTTPException(status_code=404, detail="Agent run not found")
    return _with_connection(lambda connection: get_agent_run_artifacts(connection, agent_run_id), ensure=True)


@app.get("/api/agent-runs/{agent_run_id}/proposals")
def get_agent_run_proposal_items(agent_run_id: str):
    run = _with_connection(lambda connection: get_agent_run(connection, agent_run_id), ensure=True)
    if not run:
        raise HTTPException(status_code=404, detail="Agent run not found")
    return _with_connection(lambda connection: get_agent_run_proposals(connection, agent_run_id), ensure=True)


@app.post("/api/agent-runs/{agent_run_id}/retry")
def retry_agent_run_endpoint(agent_run_id: str):
    retried = _with_connection(lambda connection: retry_agent_run(connection, agent_run_id), ensure=True)
    if not retried:
        raise HTTPException(status_code=404, detail="Agent run not found")
    run_agent_run.delay(retried["id"])
    return retried


@app.post("/api/agent-runs/{agent_run_id}/cancel")
def cancel_agent_run_endpoint(agent_run_id: str):
    cancelled = _with_connection(lambda connection: cancel_agent_run(connection, agent_run_id), ensure=True)
    if not cancelled:
        raise HTTPException(status_code=404, detail="Agent run not found")
    return cancelled


@app.post("/api/agent-reviews/{proposal_id}/approve")
def approve_agent_review(proposal_id: str):
    proposal = _with_connection(lambda connection: get_agent_proposal(connection, proposal_id), ensure=True)
    if not proposal:
        raise HTTPException(status_code=404, detail="Agent proposal not found")
    review = _with_connection(
        lambda connection: resolve_review(connection, proposal_id, True, reviewer="operator"),
        ensure=True,
    )
    promoted_result = None
    if proposal["proposal_type"] in {"ownership_promotion_bundle", "lead_proposal"}:
        def promote(connection):
            result = promote_proposal(connection, proposal)
            mark_proposal_promoted(connection, proposal_id)
            return result

        promoted_result = _with_connection(promote, ensure=True)
    return {"review": review, "promoted_result": promoted_result}


@app.post("/api/agent-reviews/{proposal_id}/reject")
def reject_agent_review(proposal_id: str):
    proposal = _with_connection(lambda connection: get_agent_proposal(connection, proposal_id), ensure=True)
    if not proposal:
        raise HTTPException(status_code=404, detail="Agent proposal not found")
    review = _with_connection(
        lambda connection: resolve_review(connection, proposal_id, False, reviewer="operator"),
        ensure=True,
    )
    return {"review": review}
