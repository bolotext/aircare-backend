import pytest

from common.enums import Role
from tests.factories import auth_header, code, data, make_org, make_user

pytestmark = pytest.mark.asyncio


async def test_admin_can_list_and_get_users_across_orgs(client):
    org_a = await make_org("Org A", "org-a")
    org_b = await make_org("Org B", "org-b")
    admin = await make_user(None, Role.ADMIN, "admin-a@example.com")
    supervisor_a = await make_user(org_a.id, Role.SUPERVISOR, "supervisor-a@example.com")
    viewer_b = await make_user(org_b.id, Role.VIEWER, "viewer-b@example.com")

    list_resp = await client.get("/api/v1/users", headers=auth_header(admin))
    assert list_resp.status_code == 200
    assert code(list_resp) == 200
    emails = {u["email"] for u in data(list_resp)}
    assert {"admin-a@example.com", "supervisor-a@example.com", "viewer-b@example.com"} <= emails

    get_resp = await client.get(f"/api/v1/users/{viewer_b.id}", headers=auth_header(admin))
    assert get_resp.status_code == 200
    assert code(get_resp) == 200
    assert data(get_resp)["email"] == "viewer-b@example.com"

    _ = supervisor_a


async def test_viewer_cannot_list_or_get_users(client):
    org = await make_org("Org C", "org-c")
    viewer = await make_user(org.id, Role.VIEWER, "viewer@example.com")
    other = await make_user(org.id, Role.SUPERVISOR, "supervisor-c@example.com")

    list_resp = await client.get("/api/v1/users", headers=auth_header(viewer))
    assert list_resp.status_code == 200
    assert code(list_resp) == 403

    get_resp = await client.get(f"/api/v1/users/{other.id}", headers=auth_header(viewer))
    assert get_resp.status_code == 200
    assert code(get_resp) == 403

    me_resp = await client.get("/api/v1/users/me", headers=auth_header(viewer))
    assert me_resp.status_code == 200
    assert code(me_resp) == 200
    assert data(me_resp)["email"] == "viewer@example.com"


async def test_supervisor_has_no_user_management_access(client):
    org = await make_org("Org D", "org-d")
    supervisor = await make_user(org.id, Role.SUPERVISOR, "supervisor-d@example.com")

    list_resp = await client.get("/api/v1/users", headers=auth_header(supervisor))
    assert list_resp.status_code == 200
    assert code(list_resp) == 403

    create_resp = await client.post(
        "/api/v1/users",
        headers=auth_header(supervisor),
        json={"email": "new@example.com", "password": "password123", "full_name": "New", "role": "VIEWER"},
    )
    assert create_resp.status_code == 200
    assert code(create_resp) == 403

    patch_resp = await client.patch(
        f"/api/v1/users/{supervisor.id}", headers=auth_header(supervisor), json={"full_name": "Renamed"}
    )
    assert patch_resp.status_code == 200
    assert code(patch_resp) == 403


async def test_admin_can_create_and_disable_user_in_any_org(client):
    org = await make_org("Org E", "org-e")
    admin = await make_user(None, Role.ADMIN, "admin-e@example.com")

    create_resp = await client.post(
        "/api/v1/users",
        headers=auth_header(admin),
        json={
            "email": "supervisor-e@example.com",
            "password": "password123",
            "full_name": "Supervisor E",
            "role": "SUPERVISOR",
            "organization_id": str(org.id),
        },
    )
    assert create_resp.status_code == 200
    assert code(create_resp) == 201
    created = data(create_resp)
    assert created["organization_id"] == str(org.id)

    disable_resp = await client.delete(f"/api/v1/users/{created['id']}", headers=auth_header(admin))
    assert disable_resp.status_code == 200
    assert code(disable_resp) == 200
    assert data(disable_resp)["status"] == "disabled"


async def test_create_user_requires_organization_id(client):
    admin = await make_user(None, Role.ADMIN, "admin-i@example.com")

    create_resp = await client.post(
        "/api/v1/users",
        headers=auth_header(admin),
        json={"email": "sneaky@example.com", "password": "password123", "full_name": "Sneaky", "role": "SUPERVISOR"},
    )
    assert create_resp.status_code == 200
    assert code(create_resp) == 400


async def test_role_escalation_to_admin_is_rejected(client):
    org = await make_org("Org F", "org-f")
    admin = await make_user(None, Role.ADMIN, "admin-f@example.com")
    resp = await client.post(
        "/api/v1/users",
        headers=auth_header(admin),
        json={
            "email": "x@example.com",
            "password": "password123",
            "full_name": "X",
            "role": "ADMIN",
            "organization_id": str(org.id),
        },
    )
    assert resp.status_code == 200
    assert code(resp) == 400


async def test_self_profile_password_change_requires_current_password(client):
    org = await make_org("Org K", "org-k")
    supervisor = await make_user(org.id, Role.SUPERVISOR, "supervisor-k@example.com", password="password123")

    wrong_resp = await client.patch(
        "/api/v1/users/me",
        headers=auth_header(supervisor),
        json={"password": "newpassword123", "current_password": "wrong"},
    )
    assert wrong_resp.status_code == 200
    assert code(wrong_resp) == 401

    ok_resp = await client.patch(
        "/api/v1/users/me",
        headers=auth_header(supervisor),
        json={"password": "newpassword123", "current_password": "password123"},
    )
    assert ok_resp.status_code == 200
    assert code(ok_resp) == 200

    login_resp = await client.post(
        "/api/v1/auth/login", json={"email": "supervisor-k@example.com", "password": "newpassword123"}
    )
    assert login_resp.status_code == 200
