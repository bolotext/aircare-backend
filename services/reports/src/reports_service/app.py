import logging
from contextlib import asynccontextmanager

import httpx
from common.errors import register_exception_handlers
from common.openapi import API_ENVELOPE_NOTE
from common.responses import success_response
from common.schemas import SuccessResponse
from fastapi import FastAPI

from reports_service.calls.router import router as calls_router
from reports_service.core.config import get_settings
from reports_service.dashboard.router import router as dashboard_router
from reports_service.db.mongodb import close_db, get_client, init_db
from reports_service.export.router import router as export_router
from reports_service.patient_calls.router import router as patient_calls_router
from reports_service.customer_details.router import router as customer_details_router
from reports_service.patients.router import router as patients_router
from reports_service.reports.router import router as reports_router
from reports_service.risk_thresholds.router import router as risk_thresholds_router

settings = get_settings()
logging.basicConfig(level=settings.log_level)

_DESCRIPTION = f"""
RBAC-gated call reporting for Manappuram: paginated call list, call detail
(transcript, event timeline, tool results), dashboard metrics, CSV export,
the daily automailer (scheduled HTML summary email, manual trigger,
recipient configuration), project-scoped patient roster CRUD, and (Admin
only) the condition -> AQI risk-threshold reference table.

Every request must carry a bearer access token issued by the Users service
(`POST /api/v1/auth/login` there). Use the **Authorize** button above to set
it once for every request this page sends.

Each project reads its call data from its own configured Mongo
database/collection (see `report_db_name`/`report_collection_name` on the
Users service's project record), so `project_id` is a required query
parameter on `/calls`, `/dashboard`, and `/export.csv` — there is no single
"all projects" view.
{API_ENVELOPE_NOTE}
"""

_TAGS_METADATA = [
    {"name": "calls", "description": "Browse and inspect individual call records for a project."},
    {"name": "dashboard", "description": "Aggregate call metrics (volume, answer rate, dispositions, ...) for a project."},
    {"name": "export", "description": "Download the same metrics and call data shown on the dashboard as a CSV file."},
    {"name": "automailer", "description": "Configure who receives the daily report email, and trigger/inspect report runs."},
    {"name": "patients", "description": "CRUD for a project's patient roster."},
    {"name": "customer-details", "description": "CRUD for a project's customer-details records (personal data), shaped by the project's configured field schema."},
    {"name": "patient-calls", "description": "CRUD for AQI-triggered patient advisory calls."},
    {"name": "risk-thresholds", "description": "Admin-only CRUD for the condition -> AQI risk-threshold reference table."},
    {"name": "health", "description": "Service liveness check."},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db(get_client())
    app.state.users_http_client = httpx.AsyncClient(base_url=settings.users_service_url)
    yield
    await app.state.users_http_client.aclose()
    await close_db()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Manappuram Reports Service",
        description=_DESCRIPTION,
        version="1.0.0",
        openapi_tags=_TAGS_METADATA,
        lifespan=lifespan,
    )
    register_exception_handlers(app)

    api_v1 = "/api/v1"
    app.include_router(calls_router, prefix=api_v1)
    app.include_router(dashboard_router, prefix=api_v1)
    app.include_router(export_router, prefix=api_v1)
    app.include_router(reports_router, prefix=api_v1)
    app.include_router(patients_router, prefix=api_v1)
    app.include_router(customer_details_router, prefix=api_v1)
    app.include_router(patient_calls_router, prefix=api_v1)
    app.include_router(risk_thresholds_router, prefix=api_v1)

    @app.get(
        "/health",
        tags=["health"],
        summary="Service health check",
        description="Unauthenticated liveness probe — returns 200 with the service name once the app has "
        "finished startup. Used by container orchestration / uptime checks, not by API clients.",
        response_model=SuccessResponse[dict],
    )
    async def health() -> dict:
        return success_response(data={"service": settings.app_name})

    return app


app = create_app()
