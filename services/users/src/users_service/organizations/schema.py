from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from users_service.enums import OrganizationStatus


class OrganizationCreate(BaseModel):
    """Creates a new tenant organization."""

    model_config = ConfigDict(json_schema_extra={"example": {"name": "Acme Insurance Co.", "slug": "acme-insurance"}})

    name: str = Field(min_length=1, max_length=200, description="Display name of the organization.")
    slug: str = Field(min_length=1, max_length=200, description="URL/identifier-safe short name, unique across organizations.")


class OrganizationUpdate(BaseModel):
    """Partial update — omitted fields are left unchanged."""

    model_config = ConfigDict(json_schema_extra={"example": {"name": "Acme Insurance Co.", "status": "active"}})

    name: str | None = Field(default=None, min_length=1, max_length=200, description="New display name. Omit to leave unchanged.")
    status: OrganizationStatus | None = Field(default=None, description="New status. Omit to leave unchanged.")


class OrganizationOut(BaseModel):
    """An organization record."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "6aae5deae9a48c4dc62aa397",
                "name": "Acme Insurance Co.",
                "slug": "acme-insurance",
                "status": "active",
                "created_by": "6aae5deae9a48c4dc62aa396",
                "created_at": "2026-09-19T10:03:22.254000",
                "updated_at": "2026-09-19T10:03:22.254000",
            }
        }
    )

    id: str = Field(description="Organization id.")
    name: str = Field(description="Display name.")
    slug: str = Field(description="URL/identifier-safe short name.")
    status: OrganizationStatus = Field(description='`"active"` or `"suspended"`.')
    created_by: str = Field(description="Id of the Admin user who created this organization.")
    created_at: datetime = Field(description="UTC creation timestamp.")
    updated_at: datetime = Field(description="UTC timestamp of the most recent update.")
