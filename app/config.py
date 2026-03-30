from dataclasses import dataclass
from pathlib import Path
import os
from typing import Optional


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    data_dir: Path
    raw_dir: Path
    tmp_dir: Path
    app_host: str
    app_port: int
    njgin_parcels_url: str
    tiger_place_zip_url: str
    redevelopment_areas_url: str
    nj_transit_light_rail_url: str
    nj_transit_station_url: str
    path_station_url: str
    essex_recorder_url: str
    nj_business_search_url: str
    overture_cli: str
    include_overture_addresses: bool
    ownership_request_delay_seconds: float
    celery_task_always_eager: bool
    ownership_fixtures_dir: Optional[Path]
    lead_fixtures_dir: Optional[Path]
    lead_source_config_path: Optional[Path]
    openai_api_key: Optional[str]
    openai_model: str
    openai_base_url: str
    openai_timeout_seconds: float
    market_id: str = "newark"
    market_name: str = "Newark"
    market_state: str = "NJ"


def get_settings():
    project_root = Path(__file__).resolve().parent.parent
    data_dir = Path(os.getenv("DATA_DIR", project_root / ".data"))
    raw_dir = data_dir / "raw"
    tmp_dir = data_dir / "tmp"
    raw_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        database_url=os.getenv(
            "DATABASE_URL",
            "postgresql://postgres:postgres@localhost:5432/market_intel",
        ),
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        data_dir=data_dir,
        raw_dir=raw_dir,
        tmp_dir=tmp_dir,
        app_host=os.getenv("MARKET_INTEL_HOST", "127.0.0.1"),
        app_port=int(os.getenv("MARKET_INTEL_PORT", "8000")),
        njgin_parcels_url=os.getenv(
            "NJGIN_PARCELS_URL",
            (
                "https://maps.nj.gov/arcgis/rest/services/Framework/Cadastral/MapServer/0/query"
            ),
        ),
        tiger_place_zip_url=os.getenv(
            "TIGER_PLACE_ZIP_URL",
            "https://www2.census.gov/geo/tiger/TIGER2025/PLACE/tl_2025_34_place.zip",
        ),
        redevelopment_areas_url=os.getenv(
            "NEWARK_REDEVELOPMENT_AREAS_URL",
            "https://services1.arcgis.com/WAUuvHqqP3le2PMh/ArcGIS/rest/services/Newark_Redevelopment_Plan_Areas/FeatureServer/0/query",
        ),
        nj_transit_light_rail_url=os.getenv(
            "NJ_TRANSIT_LIGHT_RAIL_URL",
            "https://services.arcgis.com/HggmsDF7UJsNN1FK/ArcGIS/rest/services/NJRailRoadNetworkOpenData/FeatureServer/0/query",
        ),
        nj_transit_station_url=os.getenv(
            "NJ_TRANSIT_STATION_URL",
            "https://services.arcgis.com/HggmsDF7UJsNN1FK/ArcGIS/rest/services/NJRailRoadNetworkOpenData/FeatureServer/2/query",
        ),
        path_station_url=os.getenv(
            "PATH_STATION_URL",
            "https://services.arcgis.com/HggmsDF7UJsNN1FK/ArcGIS/rest/services/NJRailRoadNetworkOpenData/FeatureServer/3/query",
        ),
        essex_recorder_url=os.getenv(
            "ESSEX_RECORDER_URL",
            "https://press.essexregister.com/prodpress/clerk/ClerkHome.aspx?op=basic",
        ),
        nj_business_search_url=os.getenv(
            "NJ_BUSINESS_SEARCH_URL",
            "https://www.njportal.com/DOR/BusinessNameSearch/Search/BusinessName",
        ),
        overture_cli=os.getenv("OVERTURE_CLI", "overturemaps"),
        include_overture_addresses=os.getenv("INCLUDE_OVERTURE_ADDRESSES", "1").strip().lower()
        in {"1", "true", "yes", "on"},
        ownership_request_delay_seconds=float(os.getenv("OWNERSHIP_REQUEST_DELAY_SECONDS", "0.5")),
        celery_task_always_eager=os.getenv("CELERY_TASK_ALWAYS_EAGER", "0").strip().lower()
        in {"1", "true", "yes", "on"},
        ownership_fixtures_dir=(
            Path(os.environ["OWNERSHIP_FIXTURES_DIR"])
            if os.getenv("OWNERSHIP_FIXTURES_DIR")
            else None
        ),
        lead_fixtures_dir=(
            Path(os.environ["LEAD_FIXTURES_DIR"])
            if os.getenv("LEAD_FIXTURES_DIR")
            else None
        ),
        lead_source_config_path=(
            Path(os.environ["LEAD_SOURCE_CONFIG_PATH"])
            if os.getenv("LEAD_SOURCE_CONFIG_PATH")
            else None
        ),
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        openai_timeout_seconds=float(os.getenv("OPENAI_TIMEOUT_SECONDS", "45")),
    )
