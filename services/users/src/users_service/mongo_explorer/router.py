"""Lets an ADMIN browse what databases/collections exist on this service's
own configured Mongo connection (MONGO_URI in .env — see
core/config.py::Settings) before typing report_db_name/report_collection_name
into POST /projects, so those two fields can be picked from a real list
instead of guessed blind.
"""

from fastapi import APIRouter, Depends, Path

from common.enums import Role
from common.errors import BadGatewayError, ForbiddenError
from common.openapi import FORBIDDEN_EXAMPLE, UNAUTHORIZED_EXAMPLE, success_and_error_responses
from common.responses import success_response
from common.schemas import SuccessResponse

from users_service.core.deps import AuthContext, require_permission
from users_service.db.mongodb import get_client
from users_service.projects import repository as project_repo

router = APIRouter(prefix="/mongo", tags=["mongo-explorer"])

# Never valid choices for report_db_name/report_collection_name — Mongo's
# own internal bookkeeping, not application data.
_SYSTEM_DB_NAMES = {"admin", "local", "config"}

_FIELD_SAMPLE_SIZE = 200  # most recent documents (by _id, newest first)

_UNREACHABLE_EXAMPLE = {
    "summary": "Mongo server unreachable",
    "value": {"status": "error", "code": 502, "message": "Unable to reach the Mongo server", "errors": {"type": "BAD_GATEWAY"}},
}


@router.get(
    "/databases",
    response_model=SuccessResponse[list[str]],
    summary="[Admin] List Mongo databases",
    description="""Lists every database on this service's own Mongo connection, excluding Mongo's internal
system databases (`admin`, `local`, `config`).

**When to use:** populating the `report_db_name` picker in a project-creation form, so it's chosen from a
real list instead of typed blind.

**Errors:** 403 if the caller lacks `project.create` (this reuses that permission rather than a
dedicated one); 502 if the Mongo server can't be reached.""",
    responses=success_and_error_responses(
        "Databases", ["aircare", "demo_collection"], UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE, _UNREACHABLE_EXAMPLE
    ),
)
async def list_databases(auth: AuthContext = Depends(require_permission("project.create"))) -> dict:
    try:
        names = await get_client().list_database_names()
    except Exception as exc:
        raise BadGatewayError("Unable to reach the Mongo server") from exc

    return success_response(data=sorted(name for name in names if name not in _SYSTEM_DB_NAMES))


@router.get(
    "/databases/{db_name}/collections",
    response_model=SuccessResponse[list[str]],
    summary="[Admin] List collections in a Mongo database",
    description="""Lists every collection in one database on this service's own Mongo connection, excluding
Mongo's internal `system.*` collections.

**When to use:** populating the `report_collection_name` picker once `report_db_name` has been chosen, so
it's chosen from a real list instead of typed blind.

**Errors:** 403 if the caller lacks `project.create`; 502 if the Mongo server can't be reached (this
includes `db_name` not existing yet — Mongo returns an empty list rather than an error for an unknown
database name, so an empty result here doesn't necessarily mean failure).""",
    responses=success_and_error_responses(
        "Collections", ["calls", "call_events", "scenario_runs"], UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE, _UNREACHABLE_EXAMPLE
    ),
)
async def list_collections(
    db_name: str = Path(..., description="The database to list collections in (see `GET /mongo/databases`).", examples=["aircare"]),
    auth: AuthContext = Depends(require_permission("project.create")),
) -> dict:
    try:
        names = await get_client()[db_name].list_collection_names()
    except Exception as exc:
        raise BadGatewayError("Unable to reach the Mongo server") from exc

    return success_response(data=sorted(name for name in names if not name.startswith("system.")))


@router.get(
    "/databases/{db_name}/collections/{collection_name}/fields",
    response_model=SuccessResponse[list[str]],
    summary="List top-level field names in a Mongo collection (Admin, or a Supervisor for their projects' sources)",
    description="""Returns the union of top-level field names seen in the 200 most recent documents (newest `_id` first). Field
names only — no values are returned, so no call data/PII leaves the database.

**When to use:** populating the raw-field picker of a project's Field Mapper.

**Errors:** 403 if the caller lacks `project.create`; 502 if the Mongo server can't be reached.""",
    responses=success_and_error_responses(
        "Field names", ["call_id", "customerPhone", "language"], UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE, _UNREACHABLE_EXAMPLE
    ),
)
async def list_fields(
    db_name: str = Path(..., description="The database (see `GET /mongo/databases`).", examples=["aircare"]),
    collection_name: str = Path(..., description="The collection to sample.", examples=["calls"]),
    auth: AuthContext = Depends(require_permission("project.update")),
) -> dict:
    if auth.role != Role.ADMIN:
        # Non-admins may only sample a source configured on a project assigned to them.
        projects = await project_repo.list_by_ids(await project_repo.list_project_ids_for_user(auth.user_id))
        allowed = {
            (p.report_db_name, c)
            for p in projects
            for c in (p.report_collection_name, p.customer_details_collection_name)
            if c
        }
        if (db_name, collection_name) not in allowed:
            raise ForbiddenError("You do not have access to this collection")
    try:
        docs = await get_client()[db_name][collection_name].find({}).sort("_id", -1).limit(_FIELD_SAMPLE_SIZE).to_list(_FIELD_SAMPLE_SIZE)
    except Exception as exc:
        raise BadGatewayError("Unable to reach the Mongo server") from exc

    return success_response(data=sorted({key for doc in docs for key in doc}))
