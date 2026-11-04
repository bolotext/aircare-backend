from beanie import PydanticObjectId
from common.enums import Role
from common.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError, UnauthorizedError

from users_service.auth import repository as refresh_token_repo
from users_service.core.deps import AuthContext
from users_service.core.security import hash_password, verify_password
from users_service.enums import UserStatus
from users_service.organizations import repository as organization_repo
from users_service.projects.model import UserProjectAssignment
from users_service.auth.model import RefreshToken
from users_service.users import repository as user_repo
from users_service.users.model import User

# Roles assignable to an org member via the Users API. ADMIN is global and is
# only ever created by the bootstrap seed script, never through this API.
ORG_ASSIGNABLE_ROLES = {Role.SUPERVISOR, Role.VIEWER}


async def create_user(
    auth: AuthContext,
    email: str,
    password: str,
    full_name: str,
    role: Role,
    organization_id: str | None,
) -> User:
    if role not in ORG_ASSIGNABLE_ROLES:
        raise BadRequestError("role must be one of SUPERVISOR, VIEWER", {"field": "role"})

    if not organization_id:
        raise BadRequestError("organization_id is required", {"field": "organization_id"})
    try:
        target_org_id = PydanticObjectId(organization_id)
    except Exception:
        raise BadRequestError("organization_id is not a valid id", {"field": "organization_id"})
    if await organization_repo.get_by_id(target_org_id) is None:
        raise NotFoundError("Organization not found")

    if await user_repo.get_by_email(email) is not None:
        raise ConflictError("A user with this email already exists", {"field": "email"})

    return await user_repo.create_user(
        email=email,
        hashed_password=hash_password(password),
        full_name=full_name,
        role=role,
        organization_id=target_org_id,
        created_by=auth.user_id,
        status=UserStatus.ACTIVE,
    )


async def list_users(
    auth: AuthContext,
    skip: int = 0,
    limit: int = 50,
    search: str | None = None,
    status: UserStatus | None = None,
) -> list[User]:
    return await user_repo.list_all(skip=skip, limit=limit, search=search, status=status)


async def get_user(auth: AuthContext, user_id: PydanticObjectId) -> User:
    user = await user_repo.get_by_id_global(user_id)
    if user is None:
        raise NotFoundError("User not found")
    return user


async def update_user(
    auth: AuthContext,
    user_id: PydanticObjectId,
    full_name: str | None,
    role: Role | None,
    status: UserStatus | None,
    password: str | None,
) -> User:
    if password and auth.user.role != Role.ADMIN:
        raise ForbiddenError("Only a super admin can reset another user's password")

    target = await user_repo.get_by_id_global(user_id)
    if target is None:
        raise NotFoundError("User not found")

    # Re-sending the current role (e.g. an ADMIN editing a profile) is a no-op; only *changing* it is restricted.
    if role is not None and role != target.role and role not in ORG_ASSIGNABLE_ROLES:
        raise BadRequestError("role must be one of SUPERVISOR, VIEWER", {"field": "role"})

    hashed_password = hash_password(password) if password else None
    updated = await user_repo.update_user(
        target, full_name=full_name, role=role, status=status, hashed_password=hashed_password
    )
    if password:
        await refresh_token_repo.revoke_all_for_user(updated.id)
    return updated


async def disable_user(auth: AuthContext, user_id: PydanticObjectId) -> User:
    target = await user_repo.get_by_id_global(user_id)
    if target is None:
        raise NotFoundError("User not found")

    updated = await user_repo.update_user(target, status=UserStatus.DISABLED)
    await refresh_token_repo.revoke_all_for_user(updated.id)
    return updated


async def delete_user(auth: AuthContext, user_id: PydanticObjectId) -> User:
    """Permanently removes a user, their project assignments and sessions. Super admin only."""
    if auth.user.role != Role.ADMIN:
        raise ForbiddenError("Only a super admin can permanently delete a user")
    target = await user_repo.get_by_id_global(user_id)
    if target is None:
        raise NotFoundError("User not found")
    if target.id == auth.user_id:
        raise BadRequestError("You can't delete your own account")
    if target.role == Role.ADMIN:
        raise BadRequestError("A super admin account can't be deleted")
    await UserProjectAssignment.find(UserProjectAssignment.user_id == target.id).delete()
    await RefreshToken.find(RefreshToken.user_id == target.id).delete()
    await target.delete()
    return target


async def update_self(
    auth: AuthContext, full_name: str | None, password: str | None, current_password: str | None
) -> User:
    user = auth.user

    if password:
        if not current_password or not verify_password(current_password, user.hashed_password):
            raise UnauthorizedError("Current password is incorrect")

    hashed_password = hash_password(password) if password else None
    updated = await user_repo.update_user(
        user, full_name=full_name, hashed_password=hashed_password, must_change_password=False if password else None
    )
    if password:
        await refresh_token_repo.revoke_all_for_user(updated.id)
    return updated
