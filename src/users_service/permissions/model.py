from datetime import datetime, timezone

from beanie import Document, PydanticObjectId
from pydantic import Field
from pymongo import IndexModel


class Permission(Document):
    """Reference record for a fine-grained permission key (e.g. "user.create",
    "user.*", "*"). Not user-editable via the API — seeded once at startup by
    db/seed.py, same as RoleDoc.
    """

    key: str
    module: str
    description: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "permissions"
        indexes = [
            IndexModel("key", unique=True),
        ]


class RolePermissionMapping(Document):
    """Grants a permission to a role, by id — a many-to-many join between the
    roles and permissions collections.
    """

    role_id: PydanticObjectId
    permission_id: PydanticObjectId
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "role_permission_mapping"
        indexes = [
            IndexModel([("role_id", 1), ("permission_id", 1)], unique=True),
        ]
