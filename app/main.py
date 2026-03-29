from functools import lru_cache

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.pipeline import build_demo_state


app = FastAPI(
    title="Architecting an Autonomous Market Intelligence System",
    description="Recruiter demo for a provenance-aware real estate intelligence pipeline.",
    version="0.1.0",
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@lru_cache(maxsize=1)
def get_demo_state():
    return build_demo_state()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    state = get_demo_state()
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "page_title": state["title"],
            "page_subtitle": state["subtitle"],
        },
    )


@app.get("/healthz")
async def healthcheck():
    return {"status": "ok"}


@app.get("/api/demo")
async def demo_data():
    state = get_demo_state()
    return {
        "title": state["title"],
        "subtitle": state["subtitle"],
        "summary": state["summary"],
        "phases": state["phases"],
        "city_summaries": state["city_summaries"],
        "properties": state["properties"],
        "targets": state["targets"],
        "source_adapters": state["source_adapters"],
        "relationship_registry": state["relationship_registry"],
    }


@app.get("/api/properties")
async def list_properties():
    return get_demo_state()["properties"]


@app.get("/api/properties/{property_id}")
async def get_property(property_id: str):
    details = get_demo_state()["property_details"].get(property_id)
    if not details:
        raise HTTPException(status_code=404, detail="Property not found")
    return details


@app.get("/api/targets")
async def list_targets():
    return get_demo_state()["targets"]


@app.get("/api/sources")
async def list_sources():
    state = get_demo_state()
    return {
        "source_adapters": state["source_adapters"],
        "source_documents": state["source_documents"],
    }
