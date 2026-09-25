import logging
from contextlib import asynccontextmanager

import httpx
from common.errors import NotFoundError, register_exception_handlers
from common.responses import success_response
from common.schemas import SuccessResponse
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from api_gateway.config import get_settings
from api_gateway.proxy import forward

settings = get_settings()
logging.basicConfig(level=settings.log_level)


def _route_map() -> dict[str, str | None]:
    """First path segment under /api/v1/ -> upstream service base URL."""
    return {
        "auth": settings.users_service_url,
        "organizations": settings.users_service_url,
        "users": settings.users_service_url,
        "projects": settings.users_service_url,
        "internal": settings.users_service_url,
        "mongo": settings.users_service_url,
        # Reports service routes are flat (no shared "/reports" prefix) —
        # each of its top-level route segments is registered individually.
        "calls": settings.reports_service_url,
        "dashboard": settings.reports_service_url,
        "export.csv": settings.reports_service_url,
        "recipients": settings.reports_service_url,
        "run": settings.reports_service_url,
        "runs": settings.reports_service_url,
        "patients": settings.reports_service_url,
        "patient-calls": settings.reports_service_url,
        "risk-thresholds": settings.reports_service_url,
        # MCP Clients service routes (identity verification + teleconsultation
        # slot booking) — the mid-call tool API the voice agent calls. The
        # Redis session-store and AQI-reconciliation MCP servers remain a
        # separate system outside this codebase and are not proxied here.
        "verify_identity": settings.mcp_clients_service_url,
        "next_slot": settings.mcp_clients_service_url,
        "reserve_slot": settings.mcp_clients_service_url,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http_client = httpx.AsyncClient(timeout=settings.upstream_timeout_seconds)
    yield
    await app.state.http_client.aclose()


_DESCRIPTION = """
Single entry point for AirCare's backend: a thin reverse proxy that forwards every `/api/v1/*` request to
the upstream microservice that owns it (Users, Reports, or MCP Clients — see the route map in
`app.py::_route_map`), based on the first path segment, and relays that service's response back unmodified.

**This gateway does not define its own request/response schemas** — it forwards raw bytes/headers, and
does no authentication or request validation itself (each upstream service still does its own JWT
verification and RBAC). For the real, documented request/response shapes of any `/api/v1/*` route, see that
upstream service's own OpenAPI docs (Users Service on its own port, Reports Service on its own port); this
page cannot show them without duplicating and risking drift from the source of truth.

**Errors:** a path with no registered upstream service returns the standard error envelope with
`code: 404`; an unreachable upstream returns `code: 502`. Any other status comes from the upstream service
itself, relayed as-is.
"""


def create_app() -> FastAPI:
    app = FastAPI(title="AirCare API Gateway", description=_DESCRIPTION, version="1.0.0", lifespan=lifespan)
    register_exception_handlers(app)

    # This gateway is the only service a browser calls directly (see the
    # route map below) — neither upstream service sets CORS headers itself.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins_list,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get(
        "/health",
        tags=["health"],
        summary="Gateway health check",
        description="Unauthenticated liveness probe for the gateway process itself — does not check "
        "whether upstream services are reachable. Used by container orchestration / uptime checks.",
        response_model=SuccessResponse[dict],
    )
    async def health() -> dict:
        return success_response(data={"service": settings.app_name})

    @app.api_route(
        "/api/v1/{full_path:path}",
        methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        tags=["proxy"],
        summary="Proxy to the owning upstream service",
        description="Forwards this request verbatim to whichever upstream service owns its first path "
        "segment (e.g. `/api/v1/calls/...` → Reports Service, `/api/v1/users/...` → Users Service) and "
        "relays the response back unmodified. See that service's own OpenAPI docs for the real "
        "request/response shape of the specific route being called.",
    )
    async def proxy_v1(full_path: str, request: Request) -> Response:
        first_segment = full_path.split("/", 1)[0]
        upstream_base = _route_map().get(first_segment)
        if not upstream_base:
            raise NotFoundError(f"No service is registered for /api/v1/{first_segment}")

        return await forward(
            request.app.state.http_client, request, upstream_base, f"/api/v1/{full_path}"
        )

    return app


app = create_app()
