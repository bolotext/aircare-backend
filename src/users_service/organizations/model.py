from datetime import datetime, timezone

from beanie import Document, PydanticObjectId
from pydantic import Field
from pymongo import IndexModel

from users_service.enums import OrganizationStatus


class Organization(Document):
    name: str
    slug: str
    status: OrganizationStatus = OrganizationStatus.ACTIVE
    created_by: PydanticObjectId
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "organizations"
        indexes = [
            IndexModel("slug", unique=True),
        ]
