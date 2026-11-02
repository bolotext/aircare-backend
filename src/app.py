"""The single FastAPI app: Users (auth/RBAC/tenancy) + Reports (calls, dashboard,
export, automailer, patients, ...) under one /api/v1, plus a thin proxy for the
two services that still run on their own (`/api/v1/mcp/*`, `/api/v1/voicebot/*`).

Reports resolves who a caller is, and where a project's data lives, through the
Users module's `/internal/*` routes. Both now live in this process, so that
call goes over an in-process ASGI transport (no network, no port, no
USERS_SERVICE_URL) — see `make_internal_client`.
"""

import logging
from contextlib import asynccontextmanager

import httpx
from common.errors import NotFoundError, register_exception_handlers
from common.openapi import API_ENVELOPE_NOTE
from common.responses import success_response
from common.schemas import SuccessResponse
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded

from gateway.config import get_settings as get_gateway_settings
from gateway.proxy import forward
from reports_service.calls.router import router as calls_router
from reports_service.customer_details.router import router as customer_details_router
from reports_service.dashboard.router import router as dashboard_router
from reports_service.db.mongodb import close_db as close_reports_db
from reports_service.db.mongodb import init_db as init_reports_db
from reports_service.export.router import router as export_router
from reports_service.patient_calls.router import router as patient_calls_router
from reports_service.patients.router import router as patients_router
from reports_service.reports.router import router as reports_router
from reports_service.risk_thresholds.router import router as risk_thresholds_router
from reports_service.s3_storage.router import router as s3_router
from users_service.auth.router import internal_router as auth_internal_router
from users_service.auth.router import router as auth_router
from users_service.core.config import get_settings
from users_service.core.rate_limit import limiter, rate_limit_exceeded_handler
from users_service.db.mongodb import close_db as close_users_db
from users_service.db.mongodb import get_client
from users_service.db.mongodb import init_db as init_users_db
from users_service.mongo_explorer.router import router as mongo_explorer_router
from users_service.organizations.router import router as organizations_router
from users_service.projects.router import router as projects_router
from users_service.users.router import router as users_router

settings = get_settings()
gateway_settings = get_gateway_settings()
logging.basicConfig(level=settings.log_level)

_DESCRIPTION = f"""
Manappuram backend: identity/RBAC/tenancy (login, organizations, users, projects and their assignments)
and RBAC-gated call reporting (call list/detail, dashboard metrics, CSV export, the daily automailer,
patient roster, customer details, risk thresholds).

Every endpoint except `POST /auth/login`, `POST /auth/refresh`, `POST /auth/logout`, `/internal/*` and
`/health` requires a bearer access token from `POST /auth/login`. Use the **Authorize** button above to set
it once for every request this page sends.

`/internal/*` is gated by a shared `X-Internal-Api-Key` header, not a user token; it is only used by this app
itself (Reports resolving identity and project config).

Each project reads its call data from its own configured Mongo database/collection, so `project_id` is a
required query parameter on `/calls`, `/dashboard` and `/export.csv`.
{API_ENVELOPE_NOTE}
"""

_TAGS_METADATA = [
    {"name": "auth", "description": "Login, token refresh/logout, and the current user's own profile."},
    {"name": "organizations", "description": "Manage tenant organizations."},
    {"name": "projects", "description": "Manage projects (each with its own report data source) and which users are assigned to them."},
    {"name": "users", "description": "Manage user accounts and roles within an organization."},
    {"name": "mongo-explorer", "description": "Browse databases/collections to help pick `report_db_name`/`report_collection_name` when creating a project."},
    {"name": "calls", "description": "Browse and inspect individual call records for a project."},
    {"name": "dashboard", "description": "Aggregate call metrics (volume, answer rate, dispositions, ...) for a project."},
    {"name": "export", "description": "Download the metrics and call data shown on the dashboard as a CSV file."},
    {"name": "automailer", "description": "Configure who receives the daily report email, and trigger/inspect report runs."},
    {"name": "patients", "description": "CRUD for a project's patient roster."},
    {"name": "customer-details", "description": "CRUD for a project's customer-details records (personal data)."},
    {"name": "patient-calls", "description": "CRUD for AQI-triggered patient advisory calls."},
    {"name": "risk-thresholds", "description": "Admin-only CRUD for the condition -> AQI risk-threshold reference table."},
    {"name": "s3", "description": "Per-project S3 connection (credentials stored encrypted) and a browser for the bucket's contents."},
    {"name": "internal", "description": "Used only by this app itself — gated by `X-Internal-Api-Key`."},
    {"name": "health", "description": "Service liveness check."},
]


async def init_databases() -> None:
    """One Mongo connection shared by the Users and Reports document models."""

    client = get_client()
    await init_users_db(client)
    await init_reports_db(client)


async def close_databases() -> None:
    await close_reports_db()
    await close_users_db()


def make_internal_client(app: FastAPI) -> httpx.AsyncClient:
    """The client Reports uses to reach the Users module's `/internal/*` routes:
    same process, no network."""

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://internal")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_databases()
    app.state.users_http_client = make_internal_client(app)
    app.state.http_client = httpx.AsyncClient(timeout=gateway_settings.upstream_timeout_seconds)
    yield
    await app.state.http_client.aclose()
    await app.state.users_http_client.aclose()
    await close_databases()


def _proxied_services() -> dict[str, str | None]:
    return {
        "mcp": gateway_settings.mcp_clients_service_url,
        "voicebot": gateway_settings.voicebot_service_url,
    }


def create_app() -> FastAPI:
    app = FastAPI(
        title="Manappuram API",
        description=_DESCRIPTION,
        version="1.0.0",
        openapi_tags=_TAGS_METADATA,
        lifespan=lifespan,
    )

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    register_exception_handlers(app)

    # A browser calls this app directly; same permissive policy the gateway had.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    api_v1 = "/api/v1"
    for router in (
        auth_router,
        organizations_router,
        mongo_explorer_router,
        projects_router,
        users_router,
        auth_internal_router,
        calls_router,
        dashboard_router,
        export_router,
        reports_router,
        patients_router,
        customer_details_router,
        patient_calls_router,
        risk_thresholds_router,
        s3_router,
    ):
        app.include_router(router, prefix=api_v1)

    @app.get(
        "/health",
        tags=["health"],
        summary="Health check",
        description="Unauthenticated liveness probe — returns 200 once the app has finished startup.",
        response_model=SuccessResponse[dict],
    )
    async def health() -> dict:
        return success_response(data={"service": settings.app_name})

    # Registered last so every real route above wins; only an otherwise-unknown
    # /api/v1/<segment> reaches it.
    @app.api_route(
        "/api/v1/{full_path:path}",
        methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        include_in_schema=False,
    )
    async def proxy_v1(full_path: str, request: Request) -> Response:
        first_segment = full_path.split("/", 1)[0]
        upstream_base = _proxied_services().get(first_segment)
        if not upstream_base:
            raise NotFoundError(f"No service is registered for /api/v1/{first_segment}")
        return await forward(request.app.state.http_client, request, upstream_base, f"/api/v1/{full_path}")

    return app


app = create_app()
