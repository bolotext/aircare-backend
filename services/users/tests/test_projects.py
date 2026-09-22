import pytest

from common.enums import Role
from tests.factories import auth_header, code, data, error_type, make_assignment, make_org, make_project, make_user

pytestmark = pytest.mark.asyncio


async def test_admin_creates_project(client):
    org = await make_org("Org P1", "org-p1")
    admin = await make_user(None, Role.ADMIN, "admin-p1@example.com")

    resp = await client.post(
        "/api/v1/projects",
        headers=auth_header(admin),
        json={
            "organization_id": str(org.id),
            "name": "Project One",
            "code": "proj-one",
            "report_db_name": "aircare",
            "report_collection_name": "calls",
        },
    )
    assert resp.status_code == 200
    assert code(resp) == 201
    body = data(resp)
    assert body["code"] == "proj-one"
    assert body["report_db_name"] == "aircare"
    assert body["report_collection_name"] == "calls"
    assert body["status"] == "active"


async def test_duplicate_project_code_in_same_org_is_rejected(client):
    org = await make_org("Org P2", "org-p2")
    admin = await make_user(None, Role.ADMIN, "admin-p2@example.com")
    payload = {
        "organization_id": str(org.id),
        "name": "Dup",
        "code": "dup-code",
        "report_db_name": "aircare",
        "report_collection_name": "calls",
    }

    first = await client.post("/api/v1/projects", headers=auth_header(admin), json=payload)
    assert code(first) == 201

    second = await client.post("/api/v1/projects", headers=auth_header(admin), json=payload)
    assert code(second) == 409
    assert error_type(second) == "CONFLICT"


async def test_non_admin_cannot_create_project(client):
    org = await make_org("Org P3", "org-p3")
    supervisor = await make_user(org.id, Role.SUPERVISOR, "supervisor-p3@example.com")

    resp = await client.post(
        "/api/v1/projects",
        headers=auth_header(supervisor),
        json={
            "organization_id": str(org.id),
            "name": "Nope",
            "code": "nope",
            "report_db_name": "aircare",
            "report_collection_name": "calls",
        },
    )
    assert code(resp) == 403


async def test_admin_sees_all_projects_supervisor_sees_assigned_only(client):
    org = await make_org("Org P4", "org-p4")
    project_a = await make_project(org.id, "Project A", "proj-a")
    await make_project(org.id, "Project B", "proj-b")
    admin = await make_user(None, Role.ADMIN, "admin-p4@example.com")
    supervisor = await make_user(org.id, Role.SUPERVISOR, "supervisor-p4@example.com")
    await make_assignment(supervisor.id, project_a.id, org.id)

    admin_resp = await client.get("/api/v1/projects", headers=auth_header(admin))
    admin_codes = {p["code"] for p in data(admin_resp)}
    assert {"proj-a", "proj-b"} <= admin_codes

    supervisor_resp = await client.get("/api/v1/projects", headers=auth_header(supervisor))
    supervisor_codes = {p["code"] for p in data(supervisor_resp)}
    assert supervisor_codes == {"proj-a"}


async def test_supervisor_gets_403_on_unassigned_project(client):
    org = await make_org("Org P5", "org-p5")
    project_a = await make_project(org.id, "Project A", "proj-a")
    project_b = await make_project(org.id, "Project B", "proj-b")
    supervisor = await make_user(org.id, Role.SUPERVISOR, "supervisor-p5@example.com")
    await make_assignment(supervisor.id, project_a.id, org.id)

    ok_resp = await client.get(f"/api/v1/projects/{project_a.id}", headers=auth_header(supervisor))
    assert code(ok_resp) == 200

    denied_resp = await client.get(f"/api/v1/projects/{project_b.id}", headers=auth_header(supervisor))
    assert code(denied_resp) == 403


async def test_admin_can_assign_and_unassign_user_to_project(client):
    org = await make_org("Org P6", "org-p6")
    project = await make_project(org.id, "Project", "proj-assign")
    admin = await make_user(None, Role.ADMIN, "admin-p6@example.com")
    viewer = await make_user(org.id, Role.VIEWER, "viewer-p6@example.com")

    assign_resp = await client.post(
        f"/api/v1/projects/{project.id}/assignments",
        headers=auth_header(admin),
        json={"user_id": str(viewer.id)},
    )
    assert code(assign_resp) == 201

    list_resp = await client.get(f"/api/v1/projects/{project.id}/assignments", headers=auth_header(admin))
    assert code(list_resp) == 200
    assert {a["user_id"] for a in data(list_resp)} == {str(viewer.id)}

    unassign_resp = await client.delete(
        f"/api/v1/projects/{project.id}/assignments/{viewer.id}", headers=auth_header(admin)
    )
    assert code(unassign_resp) == 200

    list_after_resp = await client.get(f"/api/v1/projects/{project.id}/assignments", headers=auth_header(admin))
    assert data(list_after_resp) == []


async def test_supervisor_cannot_assign_users_to_project(client):
    org = await make_org("Org P7", "org-p7")
    project = await make_project(org.id, "Project", "proj-p7")
    supervisor = await make_user(org.id, Role.SUPERVISOR, "supervisor-p7@example.com")
    viewer = await make_user(org.id, Role.VIEWER, "viewer-p7@example.com")

    resp = await client.post(
        f"/api/v1/projects/{project.id}/assignments",
        headers=auth_header(supervisor),
        json={"user_id": str(viewer.id)},
    )
    assert code(resp) == 403


async def test_assigning_user_from_different_org_is_allowed(client):
    org_a = await make_org("Org P8A", "org-p8a")
    org_b = await make_org("Org P8B", "org-p8b")
    project = await make_project(org_a.id, "Project", "proj-p8")
    admin = await make_user(None, Role.ADMIN, "admin-p8@example.com")
    outsider = await make_user(org_b.id, Role.VIEWER, "viewer-p8b@example.com")

    resp = await client.post(
        f"/api/v1/projects/{project.id}/assignments",
        headers=auth_header(admin),
        json={"user_id": str(outsider.id)},
    )
    assert code(resp) == 201

    list_resp = await client.get(f"/api/v1/projects/{project.id}/assignments", headers=auth_header(admin))
    assert {a["user_id"] for a in data(list_resp)} == {str(outsider.id)}

    # The cross-org assignee can now access the project despite belonging to a different org.
    get_resp = await client.get(f"/api/v1/projects/{project.id}", headers=auth_header(outsider))
    assert code(get_resp) == 200


async def test_assigning_an_admin_user_is_rejected(client):
    org = await make_org("Org P9", "org-p9")
    project = await make_project(org.id, "Project", "proj-p9")
    admin = await make_user(None, Role.ADMIN, "admin-p9@example.com")
    other_admin = await make_user(None, Role.ADMIN, "admin2-p9@example.com")

    resp = await client.post(
        f"/api/v1/projects/{project.id}/assignments",
        headers=auth_header(admin),
        json={"user_id": str(other_admin.id)},
    )
    assert code(resp) == 409
