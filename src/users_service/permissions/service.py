from common.enums import Role
from common.permissions import has_permission as _has_permission

from users_service.permissions.repository import PermissionRepository
from users_service.roles.repository import RoleRepository


class PermissionService:
    def __init__(self, permission_repo: PermissionRepository | None = None, role_repo: RoleRepository | None = None):
        self.permission_repo = permission_repo or PermissionRepository()
        self.role_repo = role_repo or RoleRepository()

    @staticmethod
    def has_permission(granted: set[str], required: str) -> bool:
        return _has_permission(granted, required)

    async def get_role_permissions(self, role: Role) -> set[str]:
        role_doc = await self.role_repo.get_by_name(role)
        if role_doc is None:
            return set()
        return await self.permission_repo.get_permission_keys_for_role(role_doc.id)
