"""Lets an ADMIN browse what databases/collections exist on this service's
own configured Mongo connection (MONGO_URI in .env — see
core/config.py::Settings) before typing report_db_name/report_collection_name
into POST /projects, so those two fields can be picked from a real list
instead of guessed blind.
"""

from fastapi import APIRouter, Depends, Path

from common.errors import BadGatewayError
from common.openapi import FORBIDDEN_EXAMPLE, UNAUTHORIZED_EXAMPLE, success_and_error_responses
from common.responses import success_response
from common.schemas import SuccessResponse

from users_service.core.deps import AuthContext, require_permission
from users_service.db.mongodb import get_client

router = APIRouter(prefix="/mongo", tags=["mongo-explorer"])

# Never valid choices for report_db_name/report_collection_name — Mongo's
# own internal bookkeeping, not application data.
_SYSTEM_DB_NAMES = {"admin", "local", "config"}

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
