import pytest
from beanie import PydanticObjectId

from reports_service.core.deps import get_current_identity
from tests.factories import make_call, make_identity

pytestmark = pytest.mark.asyncio

PROJECT_A = PydanticObjectId()
PROJECT_B = PydanticObjectId()
ORG = PydanticObjectId()


async def test_missing_project_id_is_rejected(app, client):
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    resp = await client.get("/api/v1/calls")
    assert resp.json()["code"] == 422


async def test_admin_still_scoped_to_the_requested_project_only(app, client):
    # Each project's report data now lives in that project's own configured
    # db/collection (see core.deps.resolve_project_source) — there is no
    # longer a single "all projects" collection to aggregate ADMIN's
    # unfiltered view from, so project_id is required even for ADMIN.
    await make_call(PROJECT_A, ORG, conversation_id="a")
    await make_call(PROJECT_B, ORG, conversation_id="b")

    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    resp = await client.get("/api/v1/calls", params={"project_id": str(PROJECT_A)})
    body = resp.json()["data"]
    assert body["total"] == 1
    assert body["items"][0]["conversation_id"] == "a"


async def test_supervisor_can_list_their_assigned_project_explicitly(app, client):
    await make_call(PROJECT_A, ORG, conversation_id="a")
    await make_call(PROJECT_B, ORG, conversation_id="b")

    app.dependency_overrides[get_current_identity] = lambda: make_identity(
        "SUPERVISOR", project_ids=[str(PROJECT_A)]
    )

    resp = await client.get("/api/v1/calls", params={"project_id": str(PROJECT_A)})
    body = resp.json()["data"]
    assert body["total"] == 1
    assert body["items"][0]["conversation_id"] == "a"


async def test_supervisor_denied_explicit_unassigned_project(app, client):
    await make_call(PROJECT_B, ORG, conversation_id="b")

    app.dependency_overrides[get_current_identity] = lambda: make_identity(
        "SUPERVISOR", project_ids=[str(PROJECT_A)]
    )

    resp = await client.get("/api/v1/calls", params={"project_id": str(PROJECT_B)})
    assert resp.json()["code"] == 403


async def test_viewer_denied_explicit_unassigned_project_on_dashboard(app, client):
    app.dependency_overrides[get_current_identity] = lambda: make_identity("VIEWER", project_ids=[str(PROJECT_A)])

    resp = await client.get("/api/v1/dashboard", params={"project_id": str(PROJECT_B)})
    assert resp.json()["code"] == 403


async def test_viewer_denied_export_even_though_report_view_works(app, client):
    await make_call(PROJECT_A, ORG, conversation_id="a")
    app.dependency_overrides[get_current_identity] = lambda: make_identity("VIEWER", project_ids=[str(PROJECT_A)])

    dashboard_resp = await client.get("/api/v1/dashboard", params={"project_id": str(PROJECT_A)})
    assert dashboard_resp.json()["status"] == "success"

    export_resp = await client.get("/api/v1/export.csv", params={"project_id": str(PROJECT_A)})
    assert export_resp.json()["code"] == 403


async def test_cross_project_call_detail_denied(app, client):
    await make_call(PROJECT_B, ORG, conversation_id="secret-call")

    app.dependency_overrides[get_current_identity] = lambda: make_identity(
        "SUPERVISOR", project_ids=[str(PROJECT_A)]
    )

    resp = await client.get("/api/v1/calls/secret-call", params={"project_id": str(PROJECT_B)})
    assert resp.json()["code"] == 403
