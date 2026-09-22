import pytest
from beanie import PydanticObjectId

from reports_service.core.deps import get_current_identity
from tests.factories import make_identity

pytestmark = pytest.mark.asyncio

PROJECT_A = PydanticObjectId()
PROJECT_B = PydanticObjectId()


async def test_supervisor_can_add_recipient_to_own_assigned_project(app, client):
    app.dependency_overrides[get_current_identity] = lambda: make_identity("SUPERVISOR", project_ids=[str(PROJECT_A)])

    resp = await client.post("/api/v1/recipients", json={"project_id": str(PROJECT_A), "email": "supervisor@example.com"})
    assert resp.json()["status"] == "success"


async def test_supervisor_cannot_add_recipient_to_other_project(app, client):
    app.dependency_overrides[get_current_identity] = lambda: make_identity("SUPERVISOR", project_ids=[str(PROJECT_A)])

    resp = await client.post("/api/v1/recipients", json={"project_id": str(PROJECT_B), "email": "x@example.com"})
    assert resp.json()["code"] == 403


async def test_supervisor_cannot_configure_a_global_recipient(app, client):
    app.dependency_overrides[get_current_identity] = lambda: make_identity("SUPERVISOR", project_ids=[str(PROJECT_A)])

    resp = await client.post("/api/v1/recipients", json={"project_id": None, "email": "x@example.com"})
    assert resp.json()["code"] == 403


async def test_admin_can_configure_a_global_recipient(app, client):
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    resp = await client.post("/api/v1/recipients", json={"project_id": None, "email": "admin@example.com"})
    assert resp.json()["status"] == "success"


async def test_viewer_denied_on_recipients_and_manual_run_endpoints(app, client):
    app.dependency_overrides[get_current_identity] = lambda: make_identity("VIEWER", project_ids=[str(PROJECT_A)])

    list_resp = await client.get("/api/v1/recipients", params={"project_id": str(PROJECT_A)})
    assert list_resp.json()["code"] == 403

    add_resp = await client.post("/api/v1/recipients", json={"project_id": str(PROJECT_A), "email": "x@example.com"})
    assert add_resp.json()["code"] == 403

    run_resp = await client.post("/api/v1/run", json={"scope": str(PROJECT_A)})
    assert run_resp.json()["code"] == 403
