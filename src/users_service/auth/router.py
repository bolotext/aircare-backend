import jwt
from beanie import PydanticObjectId
from common.enums import Role
from common.errors import NotFoundError, UnauthorizedError
from common.jwt import ACCESS_TOKEN_TYPE, decode_token
from common.openapi import error_example, not_found_example, success_and_error_responses
from common.responses import success_response
from common.schemas import SuccessResponse
from fastapi import APIRouter, Header, Path, Request
from pydantic import BaseModel, Field

from users_service.auth import service as auth_service
from users_service.auth.schema import (
    AccessTokenResponse,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    TokenResponse,
)
from users_service.core.config import get_settings
from users_service.core.rate_limit import limiter
from users_service.enums import UserStatus
from users_service.permissions.service import PermissionService
from users_service.projects import repository as project_repo
from users_service.users import repository as user_repo

router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()

_INVALID_CREDENTIALS_EXAMPLE = error_example("Wrong email or password", 401, "Invalid email or password", "UNAUTHORIZED")
_INVALID_REFRESH_TOKEN_EXAMPLE = error_example("Refresh token is invalid, expired, or revoked", 401, "Invalid or expired refresh token", "UNAUTHORIZED")


@router.post(
    "/login",
    response_model=SuccessResponse[TokenResponse],
    summary="Log in and obtain access/refresh tokens",
    description=f"""Authenticates a user by email/password and issues a short-lived access token (use it
immediately) plus a longer-lived refresh token (use it later with `POST /auth/refresh`).

**When to use:** the first call any client makes before hitting any other endpoint in this API.

**Processing behavior:** rate-limited to `{settings.login_rate_limit}` per client to slow down credential
stuffing. Does not require a bearer token — this is how one is obtained.

**Errors:** 401 if the email/password combination is wrong or the account is disabled; 429 if the rate
limit is exceeded.""",
    responses=success_and_error_responses(
        "Logged in", TokenResponse.model_config["json_schema_extra"]["example"], _INVALID_CREDENTIALS_EXAMPLE
    ),
)
@limiter.limit(settings.login_rate_limit)
async def login(request: Request, payload: LoginRequest) -> dict:
    _, access_token, refresh_token = await auth_service.login(payload.email, payload.password)
    return success_response(
        message="Logged in", data=TokenResponse(access_token=access_token, refresh_token=refresh_token)
    )


@router.post(
    "/refresh",
    response_model=SuccessResponse[AccessTokenResponse],
    summary="Exchange a refresh token for a new access token",
    description="""Mints a new access token from a still-valid refresh token, without requiring the user to
re-enter credentials.

**When to use:** when a client's access token has expired but its refresh token hasn't.

**Errors:** 401 if the refresh token is invalid, expired, or was already revoked (e.g. by `POST
/auth/logout`).""",
    responses=success_and_error_responses(
        "New access token issued", {"access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...", "token_type": "bearer"}, _INVALID_REFRESH_TOKEN_EXAMPLE
    ),
)
async def refresh(payload: RefreshRequest) -> dict:
    access_token = await auth_service.refresh_access_token(payload.refresh_token)
    return success_response(message="Access token refreshed", data=AccessTokenResponse(access_token=access_token))


@router.post(
    "/logout",
    response_model=SuccessResponse[None],
    summary="Revoke a refresh token",
    description="""Revokes a refresh token so it can no longer be exchanged for new access tokens — the
already-issued access token from the same login remains valid until it naturally expires.

**When to use:** an explicit user-initiated logout.

**Errors:** 401 if the refresh token is invalid, expired, or was already revoked (e.g. by a previous
logout) — this is not idempotent; a second logout with the same token fails.""",
    responses=success_and_error_responses("Logged out", None, _INVALID_REFRESH_TOKEN_EXAMPLE),
)
async def logout(payload: LogoutRequest) -> dict:
    await auth_service.logout(payload.refresh_token)
    return success_response(message="Logged out")


# --- Internal, service-to-service introspection -----------------------------
# Lets another internal microservice (e.g. the future Agents service) resolve
# user_id/organization_id/role from an access token without re-implementing
# JWT verification itself. Guarded by a shared internal API key, not a user
# JWT, since the caller here is a service.

internal_router = APIRouter(prefix="/internal", tags=["internal"])
_permission_service = PermissionService()

_INVALID_INTERNAL_KEY_EXAMPLE = error_example("Wrong or missing internal API key", 401, "Invalid internal API key", "UNAUTHORIZED")
_INTERNAL_API_KEY_HEADER_DESCRIPTION = "Shared service-to-service credential. Must exactly match this service's configured `internal_api_key`; never a user's bearer token."


class IntrospectRequest(BaseModel):
    """Resolves an access token to the identity/permissions/project-scope another service should enforce."""

    access_token: str = Field(description="The bearer access token to resolve (the same JWT a user would send as `Authorization: Bearer <token>`).")


class IntrospectResponse(BaseModel):
    """Everything a downstream service needs to authorize a request on this user's behalf, re-derived from
    the current database state (never trusted from the JWT's own embedded claims) — see the module note
    below on why."""

    active: bool = Field(description="Whether the token is currently valid for an active user. When `false`, every other field is unset — the caller should treat this as \"unauthenticated\".")
    user_id: str | None = Field(default=None, description="The authenticated user's id.")
    organization_id: str | None = Field(default=None, description="The user's organization id, or `null` for a global (Admin) user.")
    role: str | None = Field(default=None, description="The user's role: `ADMIN`, `SUPERVISOR`, or `VIEWER`.")
    permissions: list[str] = Field(default=[], description="The full set of fine-grained permission keys this role currently grants.")
    project_ids: list[str] | None = Field(
        default=None,
        description="The exact set of project ids this user may access, or `null` when unrestricted (`has_all_projects=true`).",
    )
    has_all_projects: bool = Field(default=False, description="`true` for an Admin (unrestricted); `false` otherwise, in which case `project_ids` is the authoritative allow-list.")


@internal_router.post(
    "/introspect",
    response_model=SuccessResponse[IntrospectResponse],
    summary="[Internal] Resolve an access token's identity and permissions",
    description="""Lets another backend service (e.g. Reports) resolve who a bearer token belongs to —
user id, organization, role, permissions, and project scope — without re-implementing JWT verification
itself. Identity/role/project-scope are re-read from the database on every call (never trusted from the
JWT's own claims), so a disable, role change, or project reassignment here takes effect on the very next
call a downstream service makes too.

**Caller:** another backend service only, authenticated via the shared `X-Internal-Api-Key` header — never
a user bearer token, and never called directly by a frontend.

**Processing behavior:** an invalid, expired, wrong-type, or otherwise unresolvable token does not raise an
error — it returns `active: false` so the caller can respond with its own 401.

**Errors:** 401 if `X-Internal-Api-Key` is missing or wrong.""",
    responses=success_and_error_responses(
        "Active user resolved",
        {
            "active": True,
            "user_id": "6aae5deae9a48c4dc62aa396",
            "organization_id": "6aae5deae9a48c4dc62aa397",
            "role": "SUPERVISOR",
            "permissions": ["report.view", "report.export"],
            "project_ids": ["6aae5deae9a48c4dc62aa398"],
            "has_all_projects": False,
        },
        _INVALID_INTERNAL_KEY_EXAMPLE,
    ),
)
async def introspect(
    payload: IntrospectRequest,
    x_internal_api_key: str | None = Header(default=None, alias="X-Internal-Api-Key", description=_INTERNAL_API_KEY_HEADER_DESCRIPTION),
) -> dict:
    if x_internal_api_key != settings.internal_api_key:
        raise UnauthorizedError("Invalid internal API key")

    try:
        claims = decode_token(payload.access_token, settings.jwt_secret, settings.jwt_algorithm)
    except jwt.InvalidTokenError:
        return success_response(data=IntrospectResponse(active=False))

    if claims.get("type") != ACCESS_TOKEN_TYPE:
        return success_response(data=IntrospectResponse(active=False))

    try:
        user_id = PydanticObjectId(claims["sub"])
    except Exception:
        return success_response(data=IntrospectResponse(active=False))

    # Re-read the user rather than trusting the JWT's embedded role/org: this
    # mirrors get_current_auth's own behavior so a disable/role-change/
    # reassignment takes effect immediately for callers of this endpoint too,
    # instead of only for direct callers of this service.
    user = await user_repo.get_by_id_global(user_id)
    if user is None or user.status != UserStatus.ACTIVE:
        return success_response(data=IntrospectResponse(active=False))

    granted = await _permission_service.get_role_permissions(user.role)

    if user.role == Role.ADMIN:
        project_ids: list[str] | None = None
        has_all_projects = True
    else:
        assigned_ids = await project_repo.list_project_ids_for_user(user.id)
        project_ids = [str(pid) for pid in assigned_ids]
        has_all_projects = False

    return success_response(
        data=IntrospectResponse(
            active=True,
            user_id=str(user.id),
            organization_id=str(user.organization_id) if user.organization_id else None,
            role=user.role.value,
            permissions=sorted(granted),
            project_ids=project_ids,
            has_all_projects=has_all_projects,
        )
    )


# --- Internal, service-to-service project config lookup ---------------------
# Lets reports-service resolve which Mongo db/collection to read a project's
# report data from, without exposing this via the user-JWT-gated
# /projects/{id} route (that route enforces project-membership on the
# calling *user*; this one is keyed by the internal API key instead, since
# the caller here is reports-service itself, acting on a project_id it has
# already permission-checked against the caller's own identity).


class InternalProjectConfig(BaseModel):
    """Where a project's report data actually lives — what the Reports service needs to pick a Mongo
    database/collection to query."""

    id: str = Field(description="The project's id (echoes the `project_id` path parameter).")
    organization_id: str = Field(description="The organization this project belongs to.")
    report_db_name: str = Field(description="Mongo database name this project's report data is stored in.", examples=["aircare"])
    report_collection_name: str = Field(description="Mongo collection name (within `report_db_name`) this project's report data is stored in.", examples=["calls"])
    customer_details_collection_name: str | None = Field(default=None, description="Collection (in `report_db_name`) holding customer details, if configured.")
    flow_id: str | None = Field(default=None, description="The prod bot's flow_id used by Import from Prod, if configured.")
    customer_details_fields: list[dict] = Field(default_factory=list, description="Key + value type of each customer-details field.")
    field_mapping: dict[str, str] = Field(default_factory=dict, description="Canonical field name -> raw field name; drives the Conversation Logs columns.")


@internal_router.get(
    "/projects/{project_id}",
    response_model=SuccessResponse[InternalProjectConfig],
    summary="[Internal] Resolve a project's report data source",
    description="""Lets the Reports service resolve which Mongo database/collection to read a project's
call data from, without going through the user-JWT-gated `GET /projects/{project_id}` route (that route
enforces the *calling user's* project membership; this one trusts the calling service, which has already
permission-checked the project_id against its own caller's identity via `POST /internal/introspect`).

**Caller:** another backend service only, authenticated via the shared `X-Internal-Api-Key` header.

**Errors:** 401 if `X-Internal-Api-Key` is missing or wrong; 404 if `project_id` doesn't exist.""",
    responses=success_and_error_responses(
        "Project data source resolved",
        {
            "id": "6aae5deae9a48c4dc62aa398",
            "organization_id": "6aae5deae9a48c4dc62aa397",
            "report_db_name": "aircare",
            "report_collection_name": "calls",
        },
        _INVALID_INTERNAL_KEY_EXAMPLE,
        not_found_example("Project"),
    ),
)
async def get_project_config(
    project_id: PydanticObjectId = Path(..., description="The project to resolve.", examples=["6aae5deae9a48c4dc62aa398"]),
    x_internal_api_key: str | None = Header(default=None, alias="X-Internal-Api-Key", description=_INTERNAL_API_KEY_HEADER_DESCRIPTION),
) -> dict:
    if x_internal_api_key != settings.internal_api_key:
        raise UnauthorizedError("Invalid internal API key")

    project = await project_repo.get_by_id(project_id)
    if project is None:
        raise NotFoundError("Project not found")

    return success_response(
        data=InternalProjectConfig(
            id=str(project.id),
            organization_id=str(project.organization_id),
            report_db_name=project.report_db_name,
            report_collection_name=project.report_collection_name,
            customer_details_collection_name=project.customer_details_collection_name,
            flow_id=project.flow_id,
            customer_details_fields=project.customer_details_fields,
            field_mapping=project.field_mapping,
        )
    )
