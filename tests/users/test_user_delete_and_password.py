import pytest

from common.enums import Role
from users_service.users.model import User
from tests.users.factories import auth_header, code, make_org, make_user

pytestmark = pytest.mark.asyncio


async def test_admin_permanently_deletes_user(client):
    org = await make_org("Org X", "org-x")
    admin = await make_user(None, Role.ADMIN, "root@example.com")
    victim = await make_user(org.id, Role.VIEWER, "victim@example.com")

    resp = await client.delete(f"/api/v1/users/{victim.id}", params={"permanent": "true"}, headers=auth_header(admin))
    assert code(resp) == 200
    assert await User.get(victim.id) is None

    assert code(await client.delete(f"/api/v1/users/{admin.id}", params={"permanent": "true"}, headers=auth_header(admin))) == 400
    other_admin = await make_user(None, Role.ADMIN, "root2@example.com")
    assert code(await client.delete(f"/api/v1/users/{other_admin.id}", params={"permanent": "true"}, headers=auth_header(admin))) == 400


async def test_default_delete_still_disables(client):
    org = await make_org("Org Y", "org-y")
    admin = await make_user(None, Role.ADMIN, "root3@example.com")
    victim = await make_user(org.id, Role.VIEWER, "v2@example.com")
    resp = await client.delete(f"/api/v1/users/{victim.id}", headers=auth_header(admin))
    assert code(resp) == 200
    assert (await User.get(victim.id)).status == "disabled"


async def test_password_reset_is_super_admin_only(client, monkeypatch):
    org = await make_org("Org Z", "org-z")
    admin = await make_user(None, Role.ADMIN, "root4@example.com")
    victim = await make_user(org.id, Role.VIEWER, "v3@example.com")
    resp = await client.patch(f"/api/v1/users/{victim.id}", headers=auth_header(admin), json={"password": "brand-new-pass"})
    assert code(resp) == 200

    # a non-admin who somehow holds user.update still can't reset passwords
    from users_service.users import service

    sup = await make_user(org.id, Role.SUPERVISOR, "s3@example.com")
    from users_service.core.deps import AuthContext
    from common.errors import ForbiddenError

    with pytest.raises(ForbiddenError):
        await service.update_user(AuthContext(user=sup), victim.id, None, None, None, "another-pass1")


async def test_admin_can_change_own_password_resending_admin_role(client):
    admin = await make_user(None, Role.ADMIN, "root5@example.com")
    resp = await client.patch(f"/api/v1/users/{admin.id}", headers=auth_header(admin), json={"role": "ADMIN", "password": "my-new-password"})
    assert code(resp) == 200
    # but changing someone to ADMIN is still refused
    org = await make_org("Org W", "org-w")
    v = await make_user(org.id, Role.VIEWER, "v5@example.com")
    assert code(await client.patch(f"/api/v1/users/{v.id}", headers=auth_header(admin), json={"role": "ADMIN"})) == 400
