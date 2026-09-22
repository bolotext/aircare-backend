from beanie import PydanticObjectId
from common.openapi import FORBIDDEN_EXAMPLE, UNAUTHORIZED_EXAMPLE, not_found_example, success_and_error_responses
from common.responses import success_response
from common.schemas import SuccessResponse
from fastapi import APIRouter, Body, Depends, Path, Query

from users_service.core.deps import AuthContext, require_permission
from users_service.enums import OrganizationStatus
from users_service.organizations import service as organization_service
from users_service.organizations.model import Organization
from users_service.organizations.schema import (
    OrganizationCreate,
    OrganizationOut,
    OrganizationUpdate,
)

router = APIRouter(prefix="/organizations", tags=["organizations"])

_ORG_EXAMPLE = OrganizationOut.model_config["json_schema_extra"]["example"]


def _to_out(org: Organization) -> OrganizationOut:
    return OrganizationOut(
        id=str(org.id),
        name=org.name,
        slug=org.slug,
        status=org.status,
        created_by=str(org.created_by),
        created_at=org.created_at,
        updated_at=org.updated_at,
    )


@router.post(
    "",
    response_model=SuccessResponse[OrganizationOut],
    summary="Create an organization",
    description="""Creates a new tenant organization.

**When to use:** onboarding a new customer/tenant.

**Required inputs:** `name`, `slug` (both required, `slug` should be unique).

**Errors:** 403 if the caller lacks `organization.create` (Admin-only in the seeded role permissions); 422
if a field is missing or too short/long.""",
    responses=success_and_error_responses("Organization created", _ORG_EXAMPLE, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE),
)
async def create_organization(
    payload: OrganizationCreate, auth: AuthContext = Depends(require_permission("organization.create"))
) -> dict:
    org = await organization_service.create_organization(
        name=payload.name,
        slug=payload.slug,
        created_by=auth.user_id,
    )
    return success_response(code=201, message="Organization created", data=_to_out(org))


@router.get(
    "",
    response_model=SuccessResponse[list[OrganizationOut]],
    summary="List organizations",
    description="""Returns organizations, optionally filtered and paginated.

**When to use:** an Admin-facing organization directory/picker.

**Errors:** 403 if the caller lacks `organization.view`.""",
    responses=success_and_error_responses("Organizations", [_ORG_EXAMPLE], UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE),
)
async def list_organizations(
    skip: int = Query(default=0, ge=0, description="Number of records to skip, for pagination."),
    limit: int = Query(default=50, ge=1, description="Maximum number of records to return."),
    search: str | None = Query(default=None, description="Case-insensitive substring match against organization name.", examples=["acme"]),
    status: OrganizationStatus | None = Query(default=None, description='Filter by status: `"active"` or `"suspended"`.'),
    auth: AuthContext = Depends(require_permission("organization.view")),
) -> dict:
    orgs = await organization_service.list_organizations(skip=skip, limit=limit, search=search, status=status)
    return success_response(data=[_to_out(o) for o in orgs])


@router.get(
    "/{org_id}",
    response_model=SuccessResponse[OrganizationOut],
    summary="Get an organization by id",
    description="""Returns a single organization's detail.

**Errors:** 403 if the caller lacks `organization.view`; 404 if `org_id` doesn't exist.""",
    responses=success_and_error_responses("Organization found", _ORG_EXAMPLE, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE, not_found_example("Organization")),
)
async def get_organization(
    org_id: PydanticObjectId = Path(..., description="The organization to fetch.", examples=["6aae5deae9a48c4dc62aa397"]),
    auth: AuthContext = Depends(require_permission("organization.view")),
) -> dict:
    org = await organization_service.get_organization(org_id)
    return success_response(data=_to_out(org))


@router.patch(
    "/{org_id}",
    response_model=SuccessResponse[OrganizationOut],
    summary="Update an organization",
    description="""Partially updates an organization — only the fields present in the request body are
changed; anything omitted (or `null`) is left as-is.

**When to use:** renaming an organization, or suspending/reactivating it.

**Errors:** 403 if the caller lacks `organization.update`; 404 if `org_id` doesn't exist.""",
    responses=success_and_error_responses("Organization updated", _ORG_EXAMPLE, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE, not_found_example("Organization")),
)
async def update_organization(
    org_id: PydanticObjectId = Path(..., description="The organization to update.", examples=["6aae5deae9a48c4dc62aa397"]),
    payload: OrganizationUpdate = Body(...),
    auth: AuthContext = Depends(require_permission("organization.update")),
) -> dict:
    org = await organization_service.update_organization(org_id, name=payload.name, status=payload.status)
    return success_response(message="Organization updated", data=_to_out(org))
