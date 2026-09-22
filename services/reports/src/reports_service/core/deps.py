from collections.abc import Callable
from dataclasses import dataclass

import httpx
from common.errors import ForbiddenError, UnauthorizedError, UnprocessableEntityError
from common.permissions import has_permission
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from motor.motor_asyncio import AsyncIOMotorCollection

from reports_service.calls.schema_adapters import UnrecognizedReportSchemaError, resolve_adapter
from reports_service.core.users_client import IdentityContext, get_project_config, introspect
from reports_service.db.mongodb import get_client

_bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_identity(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> IdentityContext:
    if credentials is None:
        raise UnauthorizedError("Missing bearer token")
    return await introspect(request.app.state.users_http_client, credentials.credentials)


def require_permission(permission: str):
    async def _checker(identity: IdentityContext = Depends(get_current_identity)) -> IdentityContext:
        if not has_permission(identity.permissions, permission):
            raise ForbiddenError("You do not have permission to perform this action")
        return identity

    return _checker


def ensure_project_access(identity: IdentityContext, project_id: str) -> None:
    if identity.has_all_projects:
        return
    if identity.project_ids is None or project_id not in identity.project_ids:
        raise ForbiddenError("You do not have access to this project")


async def resolve_collection_for_project(http_client: httpx.AsyncClient, project_id: str) -> AsyncIOMotorCollection:
    """Resolves a project's own configured report db/collection (see
    users_service.projects.model.Project) and returns the raw Mongo
    collection to query, reusing the single Mongo connection this service
    already holds (see db.mongodb.get_client) rather than opening a new one.

    Used by any non-request caller — the automailer pipeline, called both
    from the manual `/run` route and the standalone scheduler process —
    that needs the same per-project collection resolution but has an
    httpx.AsyncClient instead of a Request to resolve it from. Doesn't
    resolve a schema adapter the way resolve_project_source (below) does:
    the automailer only reads native-Call-schema collections today.
    """

    config = await get_project_config(http_client, project_id)
    return get_client()[config.report_db_name][config.report_collection_name]


@dataclass(frozen=True)
class ProjectCallSource:
    """Everything a route needs to read a project's configured report data,
    whatever shape it's actually in. `adapter` is None when the project's
    collection is already in this service's own Call schema (the common
    case — e.g. `aircare.calls`); otherwise it's the normalizer (see
    calls.schema_adapters) that turns each raw document from this
    collection into that schema, which needs `organization_id` (the source
    schema itself doesn't carry it) to do so — see calls.repository's
    adapter-aware query path.
    """

    collection: AsyncIOMotorCollection
    organization_id: str
    adapter: Callable[..., dict] | None


async def resolve_project_source(request: Request, project_id: str) -> ProjectCallSource:
    """Resolves the project's own configured report db/collection (see
    users_service.projects.model.Project) — reusing the single Mongo
    connection this service already holds (see db.mongodb.get_client)
    rather than opening a new one — plus, when that collection isn't
    already in this service's own Call schema, the adapter that normalizes
    it into one. Which adapter (if any) applies is detected from the
    collection's actual documents (see calls.schema_adapters.resolve_adapter)
    rather than looked up by db/collection name, so a new project doesn't
    need a code change here as long as its collection is already in a
    recognized shape.

    Deliberately does NOT do the project-access permission check itself
    (see ensure_project_access, called separately by each route) so that
    overriding this dependency in tests — to point at a fixture's source
    instead of making a real HTTP call — can never accidentally skip that
    check too.
    """

    config = await get_project_config(request.app.state.users_http_client, project_id)
    collection = get_client()[config.report_db_name][config.report_collection_name]
    try:
        adapter = await resolve_adapter(collection)
    except UnrecognizedReportSchemaError as exc:
        raise UnprocessableEntityError(
            "This project's configured report collection is misconfigured: its data doesn't match a "
            "recognized schema.",
            details={"report_db_name": exc.db_name, "report_collection_name": exc.collection_name},
        ) from exc
    return ProjectCallSource(collection=collection, organization_id=config.organization_id, adapter=adapter)
