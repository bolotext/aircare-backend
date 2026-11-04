from beanie import PydanticObjectId
from common.openapi import FORBIDDEN_EXAMPLE, UNAUTHORIZED_EXAMPLE, not_found_example, success_and_error_responses
from common.responses import success_response
from common.schemas import SuccessResponse
from fastapi import APIRouter, Body, Depends, Path, Query

from users_service.core.deps import AuthContext, get_current_auth, require_permission
from users_service.enums import UserStatus
from users_service.users import service as user_service
from users_service.users.model import User
from users_service.users.schema import UserCreate, UserOut, UserSelfUpdate, UserUpdate

router = APIRouter(prefix="/users", tags=["users"])

_USER_EXAMPLE = UserOut.model_config["json_schema_extra"]["example"]


def _to_out(user: User) -> UserOut:
    return UserOut(
        id=str(user.id),
        organization_id=str(user.organization_id) if user.organization_id else None,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        status=user.status,
        must_change_password=user.must_change_password,
        created_by=str(user.created_by) if user.created_by else None,
        created_at=user.created_at,
        updated_at=user.updated_at,
        last_login_at=user.last_login_at,
    )


@router.get(
    "/me",
    response_model=SuccessResponse[UserOut],
    summary="Get the current user's profile",
    description="""Returns the profile of the user the bearer token belongs to.

**When to use:** populating a "my account" screen, or checking the caller's own role after login.""",
    responses=success_and_error_responses("Current user", _USER_EXAMPLE, UNAUTHORIZED_EXAMPLE),
)
async def get_me(auth: AuthContext = Depends(get_current_auth)) -> dict:
    return success_response(data=_to_out(auth.user))


@router.patch(
    "/me",
    response_model=SuccessResponse[UserOut],
    summary="Update the current user's own profile",
    description="""Lets a user change their own display name and/or password — no special permission
needed beyond being authenticated, since it only ever touches the caller's own account.

**Processing behavior:** changing `password` requires also sending the correct `current_password`; on
success, every existing refresh token for this user is revoked (the caller must log in again on any other
device/session), and `must_change_password` is cleared.

**Errors:** 401 if `current_password` doesn't match the account's current password.""",
    responses=success_and_error_responses(
        "Profile updated", _USER_EXAMPLE, UNAUTHORIZED_EXAMPLE
    ),
)
async def update_me(payload: UserSelfUpdate, auth: AuthContext = Depends(get_current_auth)) -> dict:
    updated = await user_service.update_self(
        auth, full_name=payload.full_name, password=payload.password, current_password=payload.current_password
    )
    return success_response(message="Profile updated", data=_to_out(updated))


@router.post(
    "",
    response_model=SuccessResponse[UserOut],
    summary="Create a user",
    description="""Creates a Supervisor or Viewer user account within an organization. The global Admin
role can only be created by the service's own bootstrap seed, not through this endpoint.

**When to use:** onboarding a new team member.

**Required inputs:** `email`, `password`, `full_name`, `role`, `organization_id`.

**Errors:** 403 if the caller lacks `user.create`; 422 if a required field is missing, `password` is
shorter than 8 characters, or `email` is already in use.""",
    responses=success_and_error_responses("User created", _USER_EXAMPLE, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE),
)
async def create_user(
    payload: UserCreate, auth: AuthContext = Depends(require_permission("user.create"))
) -> dict:
    user = await user_service.create_user(
        auth,
        email=payload.email,
        password=payload.password,
        full_name=payload.full_name,
        role=payload.role,
        organization_id=payload.organization_id,
    )
    return success_response(code=201, message="User created", data=_to_out(user))


@router.get(
    "",
    response_model=SuccessResponse[list[UserOut]],
    summary="List users",
    description="""Returns user accounts, optionally filtered and paginated. Which users are visible
depends on the caller's own scope (an org-scoped caller only ever sees their own organization).

**When to use:** a user-management directory screen.

**Errors:** 403 if the caller lacks `user.view`.""",
    responses=success_and_error_responses("Users", [_USER_EXAMPLE], UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE),
)
async def list_users(
    skip: int = Query(default=0, ge=0, description="Number of records to skip, for pagination."),
    limit: int = Query(default=50, ge=1, description="Maximum number of records to return."),
    search: str | None = Query(default=None, description="Case-insensitive substring match against name/email.", examples=["priya"]),
    status: UserStatus | None = Query(default=None, description='Filter by status: `"active"`, `"invited"`, or `"disabled"`.'),
    auth: AuthContext = Depends(require_permission("user.view")),
) -> dict:
    users = await user_service.list_users(auth, skip=skip, limit=limit, search=search, status=status)
    return success_response(data=[_to_out(u) for u in users])


@router.get(
    "/{user_id}",
    response_model=SuccessResponse[UserOut],
    summary="Get a user by id",
    description="""Returns a single user's profile.

**Errors:** 403 if the caller lacks `user.view` or the user is outside the caller's scope; 404 if
`user_id` doesn't exist.""",
    responses=success_and_error_responses("User found", _USER_EXAMPLE, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE, not_found_example("User")),
)
async def get_user(
    user_id: PydanticObjectId = Path(..., description="The user to fetch.", examples=["6aae5deae9a48c4dc62aa399"]),
    auth: AuthContext = Depends(require_permission("user.view")),
) -> dict:
    user = await user_service.get_user(auth, user_id)
    return success_response(data=_to_out(user))


@router.patch(
    "/{user_id}",
    response_model=SuccessResponse[UserOut],
    summary="Update a user",
    description="""Admin/permission-gated update to another user's name, role, status, or password —
distinct from `PATCH /users/me`, which only ever updates the caller's own account.

**When to use:** changing someone's role, disabling their account via `status`, or resetting their
password (password resets are super admin only).

**Processing behavior:** `role` must be `SUPERVISOR` or `VIEWER` (an Admin role change isn't done here);
setting `password` revokes every existing refresh token for that user, forcing re-login everywhere.

**Errors:** 403 if the caller lacks `user.update` or the target user is outside the caller's scope; 404 if
`user_id` doesn't exist; 400 if `role` is set to something other than `SUPERVISOR`/`VIEWER`.""",
    responses=success_and_error_responses("User updated", _USER_EXAMPLE, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE, not_found_example("User")),
)
async def update_user(
    user_id: PydanticObjectId = Path(..., description="The user to update.", examples=["6aae5deae9a48c4dc62aa399"]),
    payload: UserUpdate = Body(...),
    auth: AuthContext = Depends(require_permission("user.update")),
) -> dict:
    user = await user_service.update_user(
        auth,
        user_id,
        full_name=payload.full_name,
        role=payload.role,
        status=payload.status,
        password=payload.password,
    )
    return success_response(message="User updated", data=_to_out(user))


@router.delete(
    "/{user_id}",
    response_model=SuccessResponse[UserOut],
    summary="Disable a user, or permanently delete with `?permanent=true` (super admin)",
    description="""By default disables a user account (soft-delete — the record is kept, not removed) so it can no
longer authenticate. Any of that user's existing access/refresh tokens stop working on their next use.

With `permanent=true` the user, their project assignments and sessions are removed for good. Super admin
(ADMIN) only; you can't delete yourself or another ADMIN.

**Errors:** 403 if the caller lacks `user.delete` or the target user is outside the caller's scope; 404 if
`user_id` doesn't exist.""",
    responses=success_and_error_responses("User disabled", _USER_EXAMPLE, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE, not_found_example("User")),
)
async def disable_user(
    user_id: PydanticObjectId = Path(..., description="The user to disable.", examples=["6aae5deae9a48c4dc62aa399"]),
    permanent: bool = Query(default=False, description="Permanently delete instead of disabling (super admin only)."),
    auth: AuthContext = Depends(require_permission("user.delete")),
) -> dict:
    if permanent:
        user = await user_service.delete_user(auth, user_id)
        return success_response(message="User deleted", data=_to_out(user))
    user = await user_service.disable_user(auth, user_id)
    return success_response(message="User disabled", data=_to_out(user))
