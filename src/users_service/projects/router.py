from beanie import PydanticObjectId
from common.errors import BadRequestError, ForbiddenError
from common.openapi import FORBIDDEN_EXAMPLE, UNAUTHORIZED_EXAMPLE, not_found_example, success_and_error_responses
from common.responses import success_response
from common.schemas import SuccessResponse
from fastapi import APIRouter, Body, Depends, Path, Query

from users_service.core.deps import AuthContext, has_permission, require_permission
from users_service.projects import service as project_service
from users_service.projects.model import Project, UserProjectAssignment
from users_service.projects.schema import (
    AssignmentCreate,
    AssignmentOut,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
)

router = APIRouter(prefix="/projects", tags=["projects"])

_PROJECT_EXAMPLE = ProjectOut.model_config["json_schema_extra"]["example"]
_ASSIGNMENT_EXAMPLE = AssignmentOut.model_config["json_schema_extra"]["example"]


def _to_out(project: Project) -> ProjectOut:
    return ProjectOut(
        id=str(project.id),
        organization_id=str(project.organization_id),
        name=project.name,
        code=project.code,
        report_db_name=project.report_db_name,
        report_collection_name=project.report_collection_name,
        status=project.status,
        customer_details_collection_name=project.customer_details_collection_name,
        flow_id=project.flow_id,
        customer_details_fields=project.customer_details_fields,
        field_mapping=project.field_mapping,
        saved_filters=project.saved_filters,
        created_by=str(project.created_by),
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


def _assignment_to_out(assignment: UserProjectAssignment) -> AssignmentOut:
    return AssignmentOut(
        user_id=str(assignment.user_id),
        project_id=str(assignment.project_id),
        organization_id=str(assignment.organization_id),
        created_by=str(assignment.created_by),
        created_at=assignment.created_at,
    )


def _parse_object_id(value: str, field: str) -> PydanticObjectId:
    try:
        return PydanticObjectId(value)
    except Exception:
        raise BadRequestError(f"{field} is not a valid id", {"field": field})


@router.post(
    "",
    response_model=SuccessResponse[ProjectOut],
    summary="Create a project",
    description="""Creates a project — a reporting scope backed by its own Mongo database/collection (see
`ProjectCreate` for how to pick those) — that users can be assigned to.

**When to use:** onboarding a new project/engagement for an organization.

**Required inputs:** `organization_id`, `name`, `code`, `report_db_name`, `report_collection_name`.

**Errors:** 403 if the caller lacks `project.create` (Admin-only in the seeded role permissions); 422 if a
required field is missing or too short/long.""",
    responses=success_and_error_responses("Project created", _PROJECT_EXAMPLE, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE),
)
async def create_project(
    payload: ProjectCreate, auth: AuthContext = Depends(require_permission("project.create"))
) -> dict:
    project = await project_service.create_project(
        auth,
        organization_id=_parse_object_id(payload.organization_id, "organization_id"),
        name=payload.name,
        code=payload.code,
        report_db_name=payload.report_db_name,
        report_collection_name=payload.report_collection_name,
        customer_details_collection_name=payload.customer_details_collection_name or None,
    )
    return success_response(code=201, message="Project created", data=_to_out(project))


@router.get(
    "",
    response_model=SuccessResponse[list[ProjectOut]],
    summary="List projects",
    description="""Returns projects, optionally filtered to one organization.

**When to use:** an organization's project picker/directory, or (Reports service context) discovering what
projects exist.

**Errors:** 403 if the caller lacks `project.view`.""",
    responses=success_and_error_responses("Projects", [_PROJECT_EXAMPLE], UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE),
)
async def list_projects(
    organization_id: str | None = Query(default=None, description="Restrict results to this organization's projects.", examples=["6aae5deae9a48c4dc62aa397"]),
    auth: AuthContext = Depends(require_permission("project.view")),
) -> dict:
    org_id = _parse_object_id(organization_id, "organization_id") if organization_id else None
    projects = await project_service.list_projects(auth, organization_id=org_id)
    return success_response(data=[_to_out(p) for p in projects])


@router.get(
    "/{project_id}",
    response_model=SuccessResponse[ProjectOut],
    summary="Get a project by id",
    description="""Returns a single project's detail, including its configured Mongo report data source.

**Errors:** 403 if the caller lacks `project.view`; 404 if `project_id` doesn't exist.""",
    responses=success_and_error_responses("Project found", _PROJECT_EXAMPLE, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE, not_found_example("Project")),
)
async def get_project(
    project_id: PydanticObjectId = Path(..., description="The project to fetch.", examples=["6aae5deae9a48c4dc62aa398"]),
    auth: AuthContext = Depends(require_permission("project.view")),
) -> dict:
    project = await project_service.get_project(auth, project_id)
    return success_response(data=_to_out(project))


@router.patch(
    "/{project_id}",
    response_model=SuccessResponse[ProjectOut],
    summary="Update a project",
    description="""Partially updates a project — only the fields present in the request body are changed. This
includes the data source (`report_db_name`, `report_collection_name`, `customer_details_collection_name`, `flow_id`).
Changing the report source does not migrate an existing `field_mapping`.

**When to use:** renaming a project, or archiving one that's no longer active.

**Errors:** 403 if the caller lacks `project.update` (Admin and Supervisor; a Supervisor only for an assigned
project, and never for the data source fields, which are Admin-only); 404 if `project_id` doesn't exist.""",
    responses=success_and_error_responses("Project updated", _PROJECT_EXAMPLE, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE, not_found_example("Project")),
)
async def update_project(
    project_id: PydanticObjectId = Path(..., description="The project to update.", examples=["6aae5deae9a48c4dc62aa398"]),
    payload: ProjectUpdate = Body(...),
    auth: AuthContext = Depends(require_permission("project.update")),
) -> dict:
    # Repointing the data source could expose another tenant's collection, so it
    # stays Admin-only; field mapping and customer fields only need project.update.
    touches_source = any(
        v is not None
        for v in (
            payload.report_db_name,
            payload.report_collection_name,
            payload.customer_details_collection_name,
            payload.flow_id,
        )
    )
    if touches_source and not await has_permission(auth, "project.create"):
        raise ForbiddenError("Only administrators can change a project's data source")
    project = await project_service.update_project(
        auth,
        project_id,
        name=payload.name,
        status=payload.status,
        field_mapping=payload.field_mapping,
        saved_filters=(
            [f.model_dump() for f in payload.saved_filters] if payload.saved_filters is not None else None
        ),
        customer_details_fields=(
            [f.model_dump() for f in payload.customer_details_fields]
            if payload.customer_details_fields is not None
            else None
        ),
        report_db_name=payload.report_db_name,
        report_collection_name=payload.report_collection_name,
        customer_details_collection_name=payload.customer_details_collection_name,
        flow_id=payload.flow_id,
    )
    return success_response(message="Project updated", data=_to_out(project))


@router.delete(
    "/{project_id}",
    response_model=SuccessResponse[None],
    summary="Delete a project",
    description="""Permanently deletes a project and all user assignments to it. **Not reversible.**

The Mongo database/collections the project read from are NOT touched, and data other services keyed by
this project id (automailer recipients/runs, patients) is left in place but becomes unreachable. Prefer
archiving (`PATCH` with `status: "archived"`) if the project may be needed again.

**Errors:** 403 if the caller lacks `project.create` (Admin-only); 404 if `project_id` doesn't exist.""",
    responses=success_and_error_responses("Project deleted", None, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE, not_found_example("Project")),
)
async def delete_project(
    project_id: PydanticObjectId = Path(..., description="The project to delete.", examples=["6aae5deae9a48c4dc62aa398"]),
    auth: AuthContext = Depends(require_permission("project.create")),
) -> dict:
    await project_service.delete_project(project_id)
    return success_response(message="Project deleted", data=None)


@router.post(
    "/{project_id}/assignments",
    response_model=SuccessResponse[AssignmentOut],
    summary="Assign a user to a project",
    description="""Grants a Supervisor/Viewer user access to a project — required before they can view that
project's calls/dashboard/reports in the Reports service. The user may belong to any organization; access is
governed entirely by this assignment, not by the user's and project's organizations matching.

**Errors:** 403 if the caller lacks `project.assign`; 404 if `project_id` doesn't exist; 409 if `user_id`
belongs to an Admin (Admins need no assignment); 422 if `user_id` isn't a valid id.""",
    responses=success_and_error_responses("User assigned", _ASSIGNMENT_EXAMPLE, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE, not_found_example("Project")),
)
async def assign_user(
    project_id: PydanticObjectId = Path(..., description="The project to grant access to.", examples=["6aae5deae9a48c4dc62aa398"]),
    payload: AssignmentCreate = Body(...),
    auth: AuthContext = Depends(require_permission("project.assign")),
) -> dict:
    assignment = await project_service.assign_user(
        auth, project_id, _parse_object_id(payload.user_id, "user_id")
    )
    return success_response(code=201, message="User assigned to project", data=_assignment_to_out(assignment))


@router.get(
    "/{project_id}/assignments",
    response_model=SuccessResponse[list[AssignmentOut]],
    summary="List a project's user assignments",
    description="""Returns every user currently assigned to a project.

**When to use:** an access-review screen for a project.

**Errors:** 403 if the caller lacks `project.assign`.""",
    responses=success_and_error_responses("Assignments", [_ASSIGNMENT_EXAMPLE], UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE),
)
async def list_assignments(
    project_id: PydanticObjectId = Path(..., description="The project to list assignments for.", examples=["6aae5deae9a48c4dc62aa398"]),
    auth: AuthContext = Depends(require_permission("project.assign")),
) -> dict:
    assignments = await project_service.list_assignments(project_id)
    return success_response(data=[_assignment_to_out(a) for a in assignments])


@router.delete(
    "/{project_id}/assignments/{user_id}",
    response_model=SuccessResponse[None],
    summary="Remove a user's project assignment",
    description="""Revokes a user's access to a project.

**When to use:** offboarding a user from a project, or correcting a mis-assignment.

**Errors:** 403 if the caller lacks `project.assign`.""",
    responses=success_and_error_responses("Assignment removed", None, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE),
)
async def unassign_user(
    project_id: PydanticObjectId = Path(..., description="The project to revoke access to.", examples=["6aae5deae9a48c4dc62aa398"]),
    user_id: PydanticObjectId = Path(..., description="The user to remove from this project.", examples=["6aae5deae9a48c4dc62aa399"]),
    auth: AuthContext = Depends(require_permission("project.assign")),
) -> dict:
    await project_service.unassign_user(project_id, user_id)
    return success_response(message="User unassigned from project")
