from beanie import PydanticObjectId
from common.errors import ConflictError, NotFoundError

from users_service.enums import OrganizationStatus
from users_service.organizations.model import Organization
from users_service.organizations import repository as organization_repo


async def create_organization(name: str, slug: str, created_by: PydanticObjectId) -> Organization:
    if await organization_repo.get_by_slug(slug) is not None:
        raise ConflictError("An organization with this slug already exists", {"field": "slug"})

    return await organization_repo.create_organization(name=name, slug=slug, created_by=created_by)


async def get_organization(organization_id: PydanticObjectId) -> Organization:
    org = await organization_repo.get_by_id(organization_id)
    if org is None:
        raise NotFoundError("Organization not found")
    return org


async def list_organizations(
    skip: int = 0,
    limit: int = 50,
    search: str | None = None,
    status: OrganizationStatus | None = None,
) -> list[Organization]:
    return await organization_repo.list_organizations(skip=skip, limit=limit, search=search, status=status)


async def update_organization(
    organization_id: PydanticObjectId, name: str | None, status: OrganizationStatus | None
) -> Organization:
    org = await get_organization(organization_id)
    return await organization_repo.update_organization(org, name=name, status=status)
