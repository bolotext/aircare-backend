from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from users_service.enums import ProjectStatus


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


class ProjectUpdate(BaseModel):
    """Partial update — omitted fields are left unchanged. `report_db_name`/`report_collection_name` are
    not editable here once a project is created."""

    model_config = ConfigDict(json_schema_extra={"example": {"name": "Demo Project A (renamed)", "status": "active"}})

    name: str | None = Field(default=None, min_length=1, max_length=200, description="New display name. Omit to leave unchanged.")
    status: ProjectStatus | None = Field(default=None, description="New status. Omit to leave unchanged.")


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
    status: ProjectStatus = Field(description='`"active"` or `"archived"`.')
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
