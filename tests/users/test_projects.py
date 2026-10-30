import pytest

from common.enums import Role
from tests.users.factories import auth_header, code, data, error_type, make_assignment, make_org, make_project, make_user

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


async def test_admin_saves_and_reads_back_field_mapping(client):
    org = await make_org("Org FM", "org-fm")
    admin = await make_user(None, Role.ADMIN, "admin-fm@example.com")
    project = await make_project(org.id, "Proj FM", "proj-fm")
    mapping = {"phone_number": "customerPhone", "call_type": "callType"}

    resp = await client.patch(
        f"/api/v1/projects/{project.id}", headers=auth_header(admin), json={"field_mapping": mapping}
    )
    assert code(resp) == 200
    assert data(resp)["field_mapping"] == mapping

    resp = await client.patch(
        f"/api/v1/projects/{project.id}", headers=auth_header(admin), json={"field_mapping": {"": "x"}}
    )
    assert code(resp) == 400


async def test_admin_edits_data_source_and_customer_details_collection(client):
    org = await make_org("Org DS", "org-ds")
    admin = await make_user(None, Role.ADMIN, "admin-ds@example.com")

    created = await client.post(
        "/api/v1/projects",
        headers=auth_header(admin),
        json={
            "organization_id": str(org.id),
            "name": "DS",
            "code": "ds",
            "report_db_name": "aircare",
            "report_collection_name": "calls",
            "customer_details_collection_name": "customers",
        },
    )
    assert code(created) == 201
    project = data(created)
    assert project["customer_details_collection_name"] == "customers"

    resp = await client.patch(
        f"/api/v1/projects/{project['id']}",
        headers=auth_header(admin),
        json={"report_collection_name": "call_logs", "customer_details_collection_name": ""},
    )
    assert code(resp) == 200
    updated = data(resp)
    assert updated["report_collection_name"] == "call_logs"
    assert updated["report_db_name"] == "aircare"
    assert updated["customer_details_collection_name"] is None


async def test_customer_details_field_schema_validation(client):
    org = await make_org("Org CF", "org-cf")
    admin = await make_user(None, Role.ADMIN, "admin-cf@example.com")
    project = await make_project(org.id, "CF", "cf")
    url = f"/api/v1/projects/{project.id}"

    ok = await client.patch(
        url, headers=auth_header(admin), json={"customer_details_fields": [{"key": "city", "type": "string"}, {"key": "age", "type": "int"}]}
    )
    assert code(ok) == 200
    assert data(ok)["customer_details_fields"] == [
        {"key": "city", "type": "string", "options": []},
        {"key": "age", "type": "int", "options": []},
    ]

    dup = await client.patch(
        url, headers=auth_header(admin), json={"customer_details_fields": [{"key": "city", "type": "text"}, {"key": "City", "type": "number"}]}
    )
    assert code(dup) == 400

    from pydantic import ValidationError

    from users_service.projects.schema import CustomerFieldSpec

    for bad in ({"key": "_id", "type": "text"}, {"key": "1abc", "type": "text"}, {"key": "x", "type": "blob"}):
        with pytest.raises(ValidationError):
            CustomerFieldSpec(**bad)


async def test_customer_field_options_validation(client):
    from pydantic import ValidationError

    from users_service.projects.schema import CustomerFieldSpec

    spec = CustomerFieldSpec(key="language", type="string", options=[" Hindi ", "English"])
    assert spec.options == ["Hindi", "English"]
    for bad in (
        {"key": "a", "type": "string", "options": ["x", "X"]},
        {"key": "a", "type": "string", "options": [""]},
        {"key": "a", "type": "bool", "options": ["yes"]},
    ):
        with pytest.raises(ValidationError):
            CustomerFieldSpec(**bad)

    org = await make_org("Org OP", "org-op")
    admin = await make_user(None, Role.ADMIN, "admin-op@example.com")
    project = await make_project(org.id, "OP", "op")
    resp = await client.patch(
        f"/api/v1/projects/{project.id}",
        headers=auth_header(admin),
        json={"customer_details_fields": [{"key": "language", "type": "string", "options": ["Hindi", "English"]}]},
    )
    assert code(resp) == 200
    assert data(resp)["customer_details_fields"][0]["options"] == ["Hindi", "English"]


async def test_admin_deletes_project_and_its_assignments(client):
    org = await make_org("Org DEL", "org-del")
    admin = await make_user(None, Role.ADMIN, "admin-del@example.com")
    supervisor = await make_user(org.id, Role.SUPERVISOR, "sup-del@example.com")
    project = await make_project(org.id, "DEL", "del")
    await make_assignment(supervisor.id, project.id, org.id)

    forbidden = await client.delete(f"/api/v1/projects/{project.id}", headers=auth_header(supervisor))
    assert code(forbidden) == 403

    resp = await client.delete(f"/api/v1/projects/{project.id}", headers=auth_header(admin))
    assert code(resp) == 200

    assert code(await client.get(f"/api/v1/projects/{project.id}", headers=auth_header(admin))) == 404
    assert code(await client.delete(f"/api/v1/projects/{project.id}", headers=auth_header(admin))) == 404
    listed = await client.get("/api/v1/projects", headers=auth_header(supervisor))
    assert data(listed) == []


async def test_cannot_create_project_in_suspended_organization(client):
    from users_service.enums import OrganizationStatus

    org = await make_org("Org SUS", "org-sus")
    org.status = OrganizationStatus.SUSPENDED
    await org.save()
    admin = await make_user(None, Role.ADMIN, "admin-sus@example.com")

    resp = await client.post(
        "/api/v1/projects",
        headers=auth_header(admin),
        json={"organization_id": str(org.id), "name": "X", "code": "x", "report_db_name": "aircare", "report_collection_name": "calls"},
    )
    assert code(resp) == 409


async def test_supervisor_edits_mapping_and_customer_fields_but_not_data_source(client):
    org = await make_org("Org SV", "org-sv")
    sup = await make_user(org.id, Role.SUPERVISOR, "sup-sv@example.com")
    project = await make_project(org.id, "Proj SV", "proj-sv")
    other = await make_project(org.id, "Proj Other", "proj-other")
    from users_service.projects import repository as project_repo_

    await project_repo_.create_assignment(sup.id, project.id, org.id, sup.id)
    url = f"/api/v1/projects/{project.id}"
    ok = await client.patch(url, headers=auth_header(sup), json={"field_mapping": {"phone_number": "p"}})
    assert code(ok) == 200
    ok = await client.patch(
        url, headers=auth_header(sup), json={"customer_details_fields": [{"key": "name", "type": "text"}]}
    )
    assert code(ok) == 200
    assert code(await client.patch(url, headers=auth_header(sup), json={"report_db_name": "x"})) == 403
    assert code(await client.patch(f"/api/v1/projects/{other.id}", headers=auth_header(sup), json={"name": "n"})) == 403
