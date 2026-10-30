from datetime import datetime, timezone

from beanie import Document
from common.enums import Role
from pydantic import Field
from pymongo import IndexModel


class RoleDoc(Document):
    """Reference record for the fixed platform roles. Not user-editable via the
    API (no router in this module) — seeded once at startup by db/seed.py so
    other services can look up role metadata without redeploying this one.
    """

    name: Role
    description: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "roles"
        indexes = [
            IndexModel("name", unique=True),
        ]
