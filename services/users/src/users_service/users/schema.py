from datetime import datetime

from common.enums import Role
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from users_service.enums import UserStatus


class UserCreate(BaseModel):
    """Creates a new user account within an organization. Only Supervisor/Viewer roles are assignable here
    — ADMIN is a global role created only by the bootstrap seed, not through this endpoint."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "email": "supervisor@example.com",
                "password": "change-me-strong-password",
                "full_name": "Priya Sharma",
                "role": "SUPERVISOR",
                "organization_id": "6aae5deae9a48c4dc62aa397",
            }
        }
    )

    email: EmailStr = Field(description="The new user's login email — must be unique.")
    password: str = Field(min_length=8, description="Initial password (at least 8 characters). The user can change it later via `PATCH /users/me`.")
    full_name: str = Field(min_length=1, max_length=200, description="Display name.")
    role: Role = Field(description='`"SUPERVISOR"` or `"VIEWER"` (creating an `"ADMIN"` here is rejected — see the endpoint description).')
    # Required: which org to create the user in. Only SUPERVISOR/VIEWER are
    # assignable here; ADMIN is global and only ever created by the bootstrap seed.
    organization_id: str | None = Field(default=None, description="The organization this user belongs to. Required for Supervisor/Viewer.")


class UserUpdate(BaseModel):
    """Admin/permission-gated update to another user's profile, role, status, or password. Omitted fields
    are left unchanged."""

    model_config = ConfigDict(json_schema_extra={"example": {"role": "VIEWER", "status": "active"}})

    full_name: str | None = Field(default=None, min_length=1, max_length=200, description="New display name. Omit to leave unchanged.")
    role: Role | None = Field(default=None, description="New role. Omit to leave unchanged.")
    status: UserStatus | None = Field(default=None, description='New status: `"active"`, `"invited"`, or `"disabled"`. Omit to leave unchanged.')
    password: str | None = Field(default=None, min_length=8, description="New password to set for this user (admin-initiated reset). Omit to leave unchanged.")


class UserSelfUpdate(BaseModel):
    """A user updating their own profile/password via `PATCH /users/me`. Changing the password requires
    confirming the current one."""

    model_config = ConfigDict(json_schema_extra={"example": {"full_name": "Priya Sharma", "password": "new-strong-password", "current_password": "change-me-strong-password"}})

    full_name: str | None = Field(default=None, min_length=1, max_length=200, description="New display name. Omit to leave unchanged.")
    password: str | None = Field(default=None, min_length=8, description="New password. Requires `current_password` to also be set.")
    current_password: str | None = Field(default=None, description="Required (and verified) when setting a new `password`.")


class UserOut(BaseModel):
    """A user account record. Never includes the password hash."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "6aae5deae9a48c4dc62aa399",
                "organization_id": "6aae5deae9a48c4dc62aa397",
                "email": "supervisor@example.com",
                "full_name": "Priya Sharma",
                "role": "SUPERVISOR",
                "status": "active",
                "must_change_password": False,
                "created_by": "6aae5deae9a48c4dc62aa396",
                "created_at": "2026-09-19T10:03:22.254000",
                "updated_at": "2026-09-19T10:03:22.254000",
                "last_login_at": "2026-09-21T08:15:00.000000",
            }
        }
    )

    id: str = Field(description="User id.")
    organization_id: str | None = Field(description="The user's organization, or `null` for a global (Admin) user.")
    email: EmailStr = Field(description="Login email.")
    full_name: str = Field(description="Display name.")
    role: Role = Field(description='`"ADMIN"`, `"SUPERVISOR"`, or `"VIEWER"`.')
    status: UserStatus = Field(description='`"active"`, `"invited"`, or `"disabled"`. A disabled user cannot authenticate.')
    must_change_password: bool = Field(description="Whether the user must set a new password before continuing (e.g. after an admin-initiated reset).")
    created_by: str | None = Field(description="Id of the user who created this account, or `null` for the bootstrap Admin.")
    created_at: datetime = Field(description="UTC creation timestamp.")
    updated_at: datetime = Field(description="UTC timestamp of the most recent update.")
    last_login_at: datetime | None = Field(description="UTC timestamp of the most recent successful login, or `null` if they've never logged in.")
