from datetime import datetime

from pydantic import BaseModel, Field


class S3ConfigIn(BaseModel):
    access_key_id: str = Field(default="", max_length=128, description="Leave blank to keep the saved key.")
    secret_access_key: str = Field(default="", max_length=256, description="Leave blank to keep the saved secret.")
    region_name: str = Field(min_length=1, max_length=64, examples=["ap-south-1"])
    bucket_name: str = Field(min_length=3, max_length=63, examples=["my-bucket"])


class S3ConfigOut(BaseModel):
    configured: bool
    access_key_hint: str | None = Field(default=None, description="Last 4 characters of the saved access key id.")
    region_name: str | None = None
    bucket_name: str | None = None
    updated_at: datetime | None = None


class S3Object(BaseModel):
    key: str
    size: int
    last_modified: str | None = None
    storage_class: str | None = None


class S3Listing(BaseModel):
    bucket: str
    prefix: str
    prefixes: list[str]
    objects: list[S3Object]
    next_token: str | None = None


class S3ObjectContent(BaseModel):
    key: str
    content_type: str | None = None
    size: int
    truncated: bool
    content: str


class S3DownloadUrl(BaseModel):
    url: str
    expires_in_seconds: int
