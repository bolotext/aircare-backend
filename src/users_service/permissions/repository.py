from beanie import PydanticObjectId

from users_service.permissions.model import Permission, RolePermissionMapping


class PermissionRepository:
    async def get_by_key(self, key: str) -> Permission | None:
        return await Permission.find_one(Permission.key == key)

    async def create(self, key: str, module: str, description: str) -> Permission:
        permission = Permission(key=key, module=module, description=description)
        await permission.insert()
        return permission

    async def find_mapping(
        self, role_id: PydanticObjectId, permission_id: PydanticObjectId
    ) -> RolePermissionMapping | None:
        return await RolePermissionMapping.find_one(
            RolePermissionMapping.role_id == role_id,
            RolePermissionMapping.permission_id == permission_id,
        )

    async def create_mapping(
        self, role_id: PydanticObjectId, permission_id: PydanticObjectId
    ) -> RolePermissionMapping:
        mapping = RolePermissionMapping(role_id=role_id, permission_id=permission_id)
        await mapping.insert()
        return mapping

    async def get_permission_keys_for_role(self, role_id: PydanticObjectId) -> set[str]:
        mappings = await RolePermissionMapping.find(RolePermissionMapping.role_id == role_id).to_list()
        permission_ids = [m.permission_id for m in mappings]
        if not permission_ids:
            return set()
        permissions = await Permission.find({"_id": {"$in": permission_ids}}).to_list()
        return {p.key for p in permissions}
