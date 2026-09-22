import re
from datetime import datetime, timezone

from beanie import PydanticObjectId
from beanie.operators import Or, RegEx

from users_service.enums import OrganizationStatus
from users_service.organizations.model import Organization


async def create_organization(name: str, slug: str, created_by: PydanticObjectId) -> Organization:
    org = Organization(name=name, slug=slug, created_by=created_by)
    await org.insert()
    return org


async def get_by_id(organization_id: PydanticObjectId) -> Organization | None:
    return await Organization.get(organization_id)


async def get_by_slug(slug: str) -> Organization | None:
    return await Organization.find_one(Organization.slug == slug)


async def list_organizations(
    skip: int = 0,
    limit: int = 50,
    search: str | None = None,
    status: OrganizationStatus | None = None,
) -> list[Organization]:
    conditions = []
    if status is not None:
        conditions.append(Organization.status == status)
    if search:
        pattern = re.escape(search)
        conditions.append(Or(RegEx(Organization.name, pattern, "i"), RegEx(Organization.slug, pattern, "i")))
    return await Organization.find(*conditions).skip(skip).limit(limit).to_list()


async def update_organization(
    org: Organization, name: str | None = None, status: OrganizationStatus | None = None
) -> Organization:
    if name is not None:
        org.name = name
    if status is not None:
        org.status = status
    org.updated_at = datetime.now(timezone.utc)
    await org.save()
    return org
