from datetime import datetime, timezone
from typing import Any

from beanie import Document, PydanticObjectId
from pydantic import Field
from pymongo import IndexModel

from users_service.enums import ProjectStatus


class Project(Document):
    organization_id: PydanticObjectId
    name: str
    code: str  # unique within the organization; a short slug
    # Where this project's report/call documents live. Reports-service reuses
    # its single Mongo connection and simply points queries at this db +
    # collection instead of a fixed shared one, so each project can bring its
    # own already-populated report data.
    report_db_name: str
    report_collection_name: str
    # Optional collection (in report_db_name) holding customer details.
    customer_details_collection_name: str | None = None
    # The prod bot's flow_id (e.g. "prod-xxxxxxxxxxxx"): sent as the `flow_id` header when importing a
    # call from prod into this project's report collection (see reports_service.calls.prod_import).
    flow_id: str | None = None
    status: ProjectStatus = ProjectStatus.ACTIVE
    # Schema of one customer-details record: [{"key": "city", "type": "text"}, ...].
    # Types are Mongo $type aliases: string|int|long|double|decimal|bool|date|objectId|array|object. See projects.schema.CustomerFieldSpec.
    customer_details_fields: list[dict[str, Any]] = Field(default_factory=list)
    # Canonical (preferred) field name -> raw field name in the report
    # collection, e.g. {"phone_number": "customerPhone"}. Stored only; the
    # reports service does not apply it yet.
    field_mapping: dict[str, str] = Field(default_factory=dict)
    # Saved Conversation Logs filter presets: [{"name", "field", "values": [...]}].
    saved_filters: list[dict[str, Any]] = Field(default_factory=list)
    created_by: PydanticObjectId
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "projects"
        indexes = [
            IndexModel([("organization_id", 1), ("code", 1)], unique=True),
            IndexModel("organization_id"),
        ]


class UserProjectAssignment(Document):
    """Grants a SUPERVISOR/VIEWER user access to one project. ADMIN needs no
    assignment rows (unrestricted, see /internal/introspect's
    has_all_projects). organization_id is denormalized from the project at
    assignment time purely as defense-in-depth for downstream services that
    query this collection directly.
    """

    user_id: PydanticObjectId
    project_id: PydanticObjectId
    organization_id: PydanticObjectId
    created_by: PydanticObjectId
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "user_project_assignments"
        indexes = [
            IndexModel([("user_id", 1), ("project_id", 1)], unique=True),
            IndexModel("project_id"),
            IndexModel("user_id"),
        ]
