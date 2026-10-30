"""Client for the Users service's `/internal/introspect` endpoint. This is
the *only* place reports-service resolves who a caller is — it never decodes
JWTs itself, so a disable/role-change/reassignment in the Users service takes
effect on the very next request here too (introspect re-reads the DB).
"""

from dataclasses import dataclass, field

import httpx
from common.errors import BadGatewayError, NotFoundError, UnauthorizedError

from reports_service.core.config import get_settings


@dataclass(frozen=True)
class IdentityContext:
    user_id: str
    organization_id: str | None
    role: str
    permissions: frozenset[str]
    # None means unrestricted (ADMIN); otherwise the exact project ids this
    # caller may touch.
    project_ids: frozenset[str] | None
    has_all_projects: bool


async def introspect(client: httpx.AsyncClient, access_token: str) -> IdentityContext:
    settings = get_settings()
    try:
        response = await client.post(
            "/api/v1/internal/introspect",
            json={"access_token": access_token},
            headers={"X-Internal-Api-Key": settings.internal_api_key},
            timeout=settings.introspect_timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise BadGatewayError("Unable to reach the identity service") from exc

    if response.status_code != 200:
        raise BadGatewayError("Identity service returned an unexpected response")

    data = (response.json() or {}).get("data") or {}
    if not data.get("active"):
        raise UnauthorizedError("Invalid or inactive session")

    project_ids = data.get("project_ids")
    return IdentityContext(
        user_id=data["user_id"],
        organization_id=data.get("organization_id"),
        role=data["role"],
        permissions=frozenset(data.get("permissions") or []),
        project_ids=frozenset(project_ids) if project_ids is not None else None,
        has_all_projects=bool(data.get("has_all_projects", False)),
    )


@dataclass(frozen=True)
class ProjectConfig:
    project_id: str
    organization_id: str
    report_db_name: str
    report_collection_name: str
    field_mapping: dict[str, str] = field(default_factory=dict)
    customer_details_collection_name: str | None = None
    flow_id: str | None = None
    customer_details_fields: list[dict[str, str]] = field(default_factory=list)


async def get_project_config(client: httpx.AsyncClient, project_id: str) -> ProjectConfig:
    """Resolves which Mongo db/collection a project's report data lives in,
    via the Users service's internal project-config endpoint (guarded by the
    same shared internal API key as introspect, since the caller here is
    reports-service itself, not a user).
    """

    settings = get_settings()
    try:
        response = await client.get(
            f"/api/v1/internal/projects/{project_id}",
            headers={"X-Internal-Api-Key": settings.internal_api_key},
            timeout=settings.introspect_timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise BadGatewayError("Unable to reach the identity service") from exc

    if response.status_code != 200:
        raise BadGatewayError("Identity service returned an unexpected response")

    body = response.json() or {}
    if body.get("status") != "success":
        raise NotFoundError("Project not found")

    data = body.get("data") or {}
    return ProjectConfig(
        project_id=data["id"],
        organization_id=data["organization_id"],
        report_db_name=data["report_db_name"],
        report_collection_name=data["report_collection_name"],
        field_mapping=data.get("field_mapping") or {},
        customer_details_collection_name=data.get("customer_details_collection_name"),
        flow_id=data.get("flow_id"),
        customer_details_fields=data.get("customer_details_fields") or [],
    )
