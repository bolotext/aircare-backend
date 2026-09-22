from common.enums import Role

from users_service.roles.model import RoleDoc


class RoleRepository:
    async def get_by_name(self, name: Role) -> RoleDoc | None:
        return await RoleDoc.find_one(RoleDoc.name == name)

    async def create(self, name: Role, description: str) -> RoleDoc:
        role = RoleDoc(name=name, description=description)
        await role.insert()
        return role
