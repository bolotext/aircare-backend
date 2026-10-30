import pytest

from common.enums import Role
from tests.users.factories import auth_header, code, data, make_assignment, make_org, make_project, make_user
from users_service.core.config import get_settings
from users_service.enums import UserStatus

pytestmark = pytest.mark.asyncio


def _internal_headers() -> dict:
    return {"X-Internal-Api-Key": get_settings().internal_api_key}


async def _introspect(client, user) -> dict:
    token = auth_header(user)["Authorization"].split(" ", 1)[1]
    resp = await client.post(
        "/api/v1/internal/introspect", json={"access_token": token}, headers=_internal_headers()
    )
    assert resp.status_code == 200
    assert code(resp) == 200
    return data(resp)


async def test_admin_introspect_is_unrestricted(client):
    admin = await make_user(None, Role.ADMIN, "admin-introspect@example.com")

    result = await _introspect(client, admin)

    assert result["active"] is True
    assert result["role"] == "ADMIN"
    assert result["has_all_projects"] is True
    assert result["project_ids"] is None
    assert "*" in result["permissions"]


async def test_supervisor_introspect_returns_assigned_projects_only(client):
    org = await make_org("Org Introspect", "org-introspect")
    project_a = await make_project(org.id, "Project A", "proj-a")
    await make_project(org.id, "Project B", "proj-b")
    supervisor = await make_user(org.id, Role.SUPERVISOR, "supervisor-introspect@example.com")
    await make_assignment(supervisor.id, project_a.id, org.id)

    result = await _introspect(client, supervisor)

    assert result["active"] is True
    assert result["role"] == "SUPERVISOR"
    assert result["has_all_projects"] is False
    assert result["project_ids"] == [str(project_a.id)]
    assert "report.view" in result["permissions"]


async def test_viewer_with_no_assignments_gets_empty_project_list(client):
    org = await make_org("Org Introspect 2", "org-introspect-2")
    viewer = await make_user(org.id, Role.VIEWER, "viewer-introspect@example.com")

    result = await _introspect(client, viewer)

    assert result["has_all_projects"] is False
    assert result["project_ids"] == []


async def test_introspect_rejects_wrong_internal_api_key(client):
    admin = await make_user(None, Role.ADMIN, "admin-introspect-2@example.com")
    token = auth_header(admin)["Authorization"].split(" ", 1)[1]

    resp = await client.post(
        "/api/v1/internal/introspect",
        json={"access_token": token},
        headers={"X-Internal-Api-Key": "wrong-key"},
    )

    assert resp.status_code == 200
    assert code(resp) == 401


async def test_introspect_reports_inactive_for_disabled_user(client):
    org = await make_org("Org Introspect 3", "org-introspect-3")
    viewer = await make_user(org.id, Role.VIEWER, "viewer-disabled@example.com", status=UserStatus.DISABLED)

    result = await _introspect(client, viewer)

    assert result["active"] is False
