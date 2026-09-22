from datetime import datetime, timezone

from beanie import Document, PydanticObjectId
from pydantic import Field
from pymongo import IndexModel


class RefreshToken(Document):
    user_id: PydanticObjectId
    token_hash: str
    expires_at: datetime
    revoked: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "refresh_tokens"
        indexes = [
            IndexModel("user_id"),
            IndexModel("token_hash", unique=True),
            IndexModel("expires_at", expireAfterSeconds=0),
        ]
