import logging
from contextlib import asynccontextmanager

from common.errors import register_exception_handlers
from common.openapi import API_ENVELOPE_NOTE
from common.responses import success_response
from common.schemas import SuccessResponse
from fastapi import FastAPI
from slowapi.errors import RateLimitExceeded

from users_service.auth.router import internal_router as auth_internal_router
from users_service.auth.router import router as auth_router
from users_service.core.config import get_settings
from users_service.core.rate_limit import limiter, rate_limit_exceeded_handler
from users_service.db.mongodb import close_db, get_client, init_db
from users_service.mongo_explorer.router import router as mongo_explorer_router
from users_service.organizations.router import router as organizations_router
from users_service.projects.router import router as projects_router
from users_service.users.router import router as users_router

settings = get_settings()
logging.basicConfig(level=settings.log_level)

_DESCRIPTION = f"""
Identity, RBAC, and tenancy for Manappuram: login/session management, organizations, users, projects, and the
project-to-user assignments that scope non-Admin access. Also the platform's single source of truth for
per-project report data location (`report_db_name`/`report_collection_name`), which the Reports service
resolves via this service's internal endpoints.

Every endpoint except `POST /auth/login`, `POST /auth/refresh`, `POST /auth/logout`, and the `/internal/*`
endpoints requires a bearer access token from `POST /auth/login`. Use the **Authorize** button above to set
it once for every request this page sends.

`/internal/*` endpoints are service-to-service only (gated by a shared `X-Internal-Api-Key` header, not a
user token) — called by other backend services (e.g. Reports), never directly by a frontend.

**Roles:** `ADMIN` (global, unrestricted), `SUPERVISOR` and `VIEWER` (org-scoped, further restricted to
their assigned projects). Most endpoints are additionally gated by a fine-grained permission
(`organization.view`, `project.create`, `user.delete`, ...) rather than the role directly.
{API_ENVELOPE_NOTE}
"""

_TAGS_METADATA = [
    {"name": "auth", "description": "Login, token refresh/logout, and the current user's own profile."},
    {"name": "organizations", "description": "Manage tenant organizations."},
    {"name": "projects", "description": "Manage projects (each with its own report data source) and which users are assigned to them."},
    {"name": "users", "description": "Manage user accounts and roles within an organization."},
    {"name": "mongo-explorer", "description": "Browse databases/collections on this service's Mongo connection, to help pick `report_db_name`/`report_collection_name` when creating a project."},
    {"name": "internal", "description": "Service-to-service only — gated by `X-Internal-Api-Key`, not a user bearer token."},
    {"name": "health", "description": "Service liveness check."},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Seeding is a separate, explicit step (`python main.py seed`) — run it
    # yourself before `serve` locally, or via services/users/entrypoint.sh in
    # Docker, which already runs `seed` then `serve`. It doesn't belong here:
    # every app boot would otherwise re-run it, which surprised local dev.
    await init_db(get_client())
    yield
    await close_db()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Manappuram Users Service",
        description=_DESCRIPTION,
        version="1.0.0",
        openapi_tags=_TAGS_METADATA,
        lifespan=lifespan,
    )

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    register_exception_handlers(app)

    api_v1 = "/api/v1"
    app.include_router(auth_router, prefix=api_v1)
    app.include_router(organizations_router, prefix=api_v1)
    app.include_router(mongo_explorer_router, prefix=api_v1)
    app.include_router(projects_router, prefix=api_v1)
    app.include_router(users_router, prefix=api_v1)
    app.include_router(auth_internal_router, prefix=api_v1)

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
