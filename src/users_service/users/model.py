from datetime import datetime, timezone

from beanie import Document, PydanticObjectId
from common.enums import Role
from pydantic import Field
from pymongo import IndexModel

from users_service.enums import UserStatus


class User(Document):
    organization_id: PydanticObjectId | None = None  # null only for ADMIN (global)
    email: str
    hashed_password: str
    full_name: str
    role: Role
    status: UserStatus = UserStatus.ACTIVE
    must_change_password: bool = False
    created_by: PydanticObjectId | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_login_at: datetime | None = None

    class Settings:
        name = "users"
        indexes = [
            # Global uniqueness (not compound with organization_id): login takes only an
            # email + password with no org context, so email must resolve unambiguously.
            IndexModel("email", unique=True),
            IndexModel("organization_id"),
        ]
