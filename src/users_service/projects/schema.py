from datetime import datetime

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from users_service.enums import ProjectStatus


_LEGACY_TYPES = {"text": "string", "number": "double", "boolean": "bool"}
# Types whose value can be limited to a fixed list (rendered as a dropdown).
_OPTION_TYPES = {"string", "int", "long", "double", "decimal"}
_RESERVED_KEYS = {"_id", "id"}
_KEY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


class CustomerFieldSpec(BaseModel):
    """One field of a project's customer-details record: the key stored in Mongo and the type of its value."""

    key: str = Field(description="Mongo field name, e.g. `city`. Letters, digits and underscores; must start with a letter.")
    type: Literal["string", "int", "long", "double", "decimal", "bool", "date", "objectId", "array", "object"] = Field(
        description="MongoDB (BSON) type of the value, using Mongo's `$type` aliases; drives validation and the stored type on write."
    )

    @field_validator("type", mode="before")
    @classmethod
    def _legacy_type(cls, value):
        # Schemas saved before Mongo types were introduced.
        return _LEGACY_TYPES.get(value, value)

    options: list[str] = Field(
        default_factory=list,
        max_length=100,
        description="Optional allowed values; when set, the customer form shows a dropdown and writes must be one of them. Only for string/int/long/double/decimal fields.",
    )

    @field_validator("options")
    @classmethod
    def _clean_options(cls, values: list[str]) -> list[str]:
        cleaned = [v.strip() for v in values]
        if any(not v or len(v) > 100 for v in cleaned):
            raise ValueError("each option must be 1-100 characters")
        if len({v.lower() for v in cleaned}) != len(cleaned):
            raise ValueError("options must be unique")
        return cleaned

    @model_validator(mode="after")
    def _options_need_scalar_type(self):
        if self.options and self.type not in _OPTION_TYPES:
            raise ValueError(f"options are not supported for type {self.type}")
        return self

    @field_validator("key")
    @classmethod
    def _valid_key(cls, value: str) -> str:
        value = value.strip()
        if not _KEY_PATTERN.match(value) or value.lower() in _RESERVED_KEYS:
            raise ValueError("key must start with a letter, use only letters/digits/underscores, and not be reserved")
        return value


class ProjectCreate(BaseModel):
    """Creates a project — a reporting scope with its own Mongo data source, that users can be assigned to.

    `report_db_name`/`report_collection_name` should be picked from `GET /mongo/databases` and `GET
    /mongo/databases/{db_name}/collections` rather than typed blind."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "organization_id": "6aae5deae9a48c4dc62aa397",
                "name": "Demo Project A",
                "code": "proj-a",
                "report_db_name": "aircare",
                "report_collection_name": "calls",
            }
        }
    )

    organization_id: str = Field(description="The organization this project belongs to.")
    name: str = Field(min_length=1, max_length=200, description="Display name of the project.")
    code: str = Field(min_length=1, max_length=50, description="Short, url-safe project code, unique within the organization.")
    # The Mongo database + collection reports-service should read this
    # project's report data from (same Mongo connection, different target).
    report_db_name: str = Field(
        min_length=1,
        max_length=100,
        description="Mongo database name the Reports service should read this project's call data from. See `GET /mongo/databases` for real options.",
    )
    report_collection_name: str = Field(
        min_length=1,
        max_length=100,
        description="Mongo collection name (within `report_db_name`) to read this project's call data from. See `GET /mongo/databases/{db_name}/collections`.",
    )
    customer_details_collection_name: str | None = Field(
        default=None,
        max_length=100,
        description="Optional collection (within `report_db_name`) holding customer details. Pick from `GET /mongo/databases/{db_name}/collections`.",
    )


class ProjectUpdate(BaseModel):
    """Partial update — omitted fields are left unchanged. The data source (`report_db_name`,
    `report_collection_name`, `customer_details_collection_name`) is editable; an empty
    `customer_details_collection_name` clears it."""

    model_config = ConfigDict(json_schema_extra={"example": {"name": "Demo Project A (renamed)", "status": "active"}})

    name: str | None = Field(default=None, min_length=1, max_length=200, description="New display name. Omit to leave unchanged.")
    status: ProjectStatus | None = Field(default=None, description="New status. Omit to leave unchanged.")
    report_db_name: str | None = Field(default=None, min_length=1, max_length=100, description="New report database. Omit to leave unchanged.")
    report_collection_name: str | None = Field(
        default=None, min_length=1, max_length=100, description="New report collection. Omit to leave unchanged."
    )
    customer_details_collection_name: str | None = Field(
        default=None, max_length=100, description="New customer-details collection; empty string clears it. Omit to leave unchanged."
    )
    flow_id: str | None = Field(
        default=None,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]*$",
        description="The prod bot's flow_id used by Import from Prod; empty string clears it. Omit to leave unchanged.",
    )
    customer_details_fields: list[CustomerFieldSpec] | None = Field(
        default=None,
        max_length=100,
        description="Replaces the whole customer-details field schema (key + value type). Omit to leave unchanged.",
    )
    field_mapping: dict[str, str] | None = Field(
        default=None,
        max_length=200,
        description="Replaces the whole canonical-name -> raw-field mapping. Omit to leave unchanged.",
    )


class ProjectOut(BaseModel):
    """A project record."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "6aae5deae9a48c4dc62aa398",
                "organization_id": "6aae5deae9a48c4dc62aa397",
                "name": "Demo Project A",
                "code": "proj-a",
                "report_db_name": "aircare",
                "report_collection_name": "calls",
                "status": "active",
                "created_by": "6aae5deae9a48c4dc62aa396",
                "created_at": "2026-09-19T10:03:22.254000",
                "updated_at": "2026-09-19T10:03:22.254000",
            }
        }
    )

    id: str = Field(description="Project id.")
    organization_id: str = Field(description="The organization this project belongs to.")
    name: str = Field(description="Display name.")
    code: str = Field(description="Short, url-safe project code.")
    report_db_name: str = Field(description="Mongo database this project's call data is read from.")
    report_collection_name: str = Field(description="Mongo collection (within `report_db_name`) this project's call data is read from.")
    customer_details_collection_name: str | None = Field(default=None, description="Collection holding customer details, if configured.")
    flow_id: str | None = Field(default=None, description="The prod bot's flow_id used by Import from Prod, if configured.")
    status: ProjectStatus = Field(description='`"active"` or `"archived"`.')
    customer_details_fields: list[CustomerFieldSpec] = Field(default_factory=list, description="Key + value type of each customer-details field.")
    field_mapping: dict[str, str] = Field(default_factory=dict, description="Canonical field name -> raw field name in the report collection.")
    created_by: str = Field(description="Id of the user who created this project.")
    created_at: datetime = Field(description="UTC creation timestamp.")
    updated_at: datetime = Field(description="UTC timestamp of the most recent update.")


class AssignmentCreate(BaseModel):
    """Grants a user access to a project."""

    model_config = ConfigDict(json_schema_extra={"example": {"user_id": "6aae5deae9a48c4dc62aa399"}})

    user_id: str = Field(description="The user (Mongo ObjectId) to grant access to this project.")


class AssignmentOut(BaseModel):
    """A user-to-project assignment record."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "user_id": "6aae5deae9a48c4dc62aa399",
                "project_id": "6aae5deae9a48c4dc62aa398",
                "organization_id": "6aae5deae9a48c4dc62aa397",
                "created_by": "6aae5deae9a48c4dc62aa396",
                "created_at": "2026-09-19T10:03:22.254000",
            }
        }
    )

    user_id: str = Field(description="The assigned user.")
    project_id: str = Field(description="The project they were assigned to.")
    organization_id: str = Field(description="The project's organization (the assigned user may belong to a different one).")
    created_by: str = Field(description="Id of the user who made this assignment.")
    created_at: datetime = Field(description="UTC timestamp the assignment was created.")
