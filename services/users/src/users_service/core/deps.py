from dataclasses import dataclass

import jwt
from beanie import PydanticObjectId
from common.enums import Role
from common.errors import ForbiddenError, UnauthorizedError
from common.log_utils import setup_logger
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from users_service.core.security import ACCESS_TOKEN_TYPE, decode_own_token
from users_service.enums import UserStatus
from users_service.permissions.service import PermissionService
from users_service.users.model import User

logger = setup_logger(__name__)

_bearer_scheme = HTTPBearer(auto_error=False)
_permission_service = PermissionService()


@dataclass(frozen=True)
class AuthContext:
    user: User

    @property
    def user_id(self) -> PydanticObjectId:
        return self.user.id

    @property
    def organization_id(self) -> PydanticObjectId | None:
        return self.user.organization_id

    @property
    def role(self) -> Role:
        return self.user.role

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN


async def get_current_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> AuthContext:
    if credentials is None:
        raise UnauthorizedError("Missing bearer token")

    try:
        payload = decode_own_token(credentials.credentials)
    except jwt.ExpiredSignatureError:
        raise UnauthorizedError("Access token has expired")
    except jwt.InvalidTokenError:
        raise UnauthorizedError("Invalid access token")

    if payload.get("type") != ACCESS_TOKEN_TYPE:
        raise UnauthorizedError("Invalid token type")

    try:
        user_id = PydanticObjectId(payload["sub"])
    except Exception:
        raise UnauthorizedError("Invalid token subject")

    # Re-read the user on every request (never trust stale JWT claims for
    # role/status): this makes disable-user and role-change take effect
    # immediately instead of waiting out the access token's lifetime.
    user = await User.get(user_id)
    if user is None or user.status != UserStatus.ACTIVE:
        raise UnauthorizedError("User is not active")

    return AuthContext(user=user)


def require_role(*allowed_roles: Role):
    async def _checker(auth: AuthContext = Depends(get_current_auth)) -> AuthContext:
        if auth.role not in allowed_roles:
            raise ForbiddenError("You do not have permission to perform this action")
        return auth

    return _checker


def require_permission(permission: str):
    """Like require_role, but checks a fine-grained permission key against the
    caller's role via RolePermissionMapping (see users_service.permissions),
    instead of hardcoding a role list at each call site.
    """

    async def _checker(auth: AuthContext = Depends(get_current_auth)) -> AuthContext:
        granted = await _permission_service.get_role_permissions(auth.role)
        if not _permission_service.has_permission(granted, permission):
            logger.warning(
                "Permission denied: role=%s required=%s granted=%s", auth.role, permission, granted
            )
            raise ForbiddenError("You do not have permission to perform this action")
        return auth

    return _checker
