"""Per-project S3 connection settings, configured from the UI.

The access key id and secret access key are stored as given (plain text) unless
`S3_CREDENTIALS_ENCRYPTION_KEY` is set, in which case new values are encrypted
(see crypto.py). Either way they are never returned by any API.
"""

from datetime import datetime, timezone

from beanie import Document, PydanticObjectId
from pydantic import Field
from pymongo import IndexModel


class S3Config(Document):
    project_id: PydanticObjectId
    access_key_id_enc: str
    secret_access_key_enc: str
    # Last 4 characters of the access key id, kept only so the UI can show
    # which key is configured without ever decrypting/returning it.
    access_key_hint: str
    region_name: str
    bucket_name: str
    updated_by: str
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "s3_configs"
        indexes = [IndexModel("project_id", unique=True)]
