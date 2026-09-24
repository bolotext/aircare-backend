"""Proves project isolation for patient-advisory calls even though the
record itself carries no project_id — access is resolved through the call's
patient_id -> patients.project_id, and must be just as strict as /patients
and /calls.
"""

import pytest
from beanie import PydanticObjectId

from reports_service.core.deps import get_current_identity
from tests.factories import make_identity, make_patient, make_patient_call

pytestmark = pytest.mark.asyncio

PROJECT_A = PydanticObjectId()
PROJECT_B = PydanticObjectId()


async def test_missing_project_id_is_rejected(app, client):
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    resp = await client.get("/api/v1/patient-calls")
    assert resp.json()["code"] == 422


async def test_supervisor_sees_only_their_projects_calls(app, client):
    patient_a = await make_patient(PROJECT_A)
    patient_b = await make_patient(PROJECT_B)
    await make_patient_call(patient_a.patient_id)
    await make_patient_call(patient_b.patient_id)

    app.dependency_overrides[get_current_identity] = lambda: make_identity("SUPERVISOR", project_ids=[str(PROJECT_A)])

    resp = await client.get("/api/v1/patient-calls", params={"project_id": str(PROJECT_A)})
    body = resp.json()["data"]
    assert body["total"] == 1
    assert body["items"][0]["patient_id"] == patient_a.patient_id

    denied = await client.get("/api/v1/patient-calls", params={"project_id": str(PROJECT_B)})
    assert denied.json()["code"] == 403


async def test_cross_project_patient_id_filter_is_not_found(app, client):
    patient_b = await make_patient(PROJECT_B)
    app.dependency_overrides[get_current_identity] = lambda: make_identity("SUPERVISOR", project_ids=[str(PROJECT_A)])

    # Filtering project A's listing by a patient that actually belongs to
    # project B must not leak that patient's calls — nor even confirm they
    # exist — via a query-param mismatch.
    resp = await client.get(
        "/api/v1/patient-calls", params={"project_id": str(PROJECT_A), "patient_id": patient_b.patient_id}
    )
    assert resp.json()["code"] == 404


async def test_cross_project_call_detail_is_not_found_not_leaked(app, client):
    patient_b = await make_patient(PROJECT_B)
    call = await make_patient_call(patient_b.patient_id)

    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    resp = await client.get(f"/api/v1/patient-calls/{call.call_id}", params={"project_id": str(PROJECT_A)})
    assert resp.json()["code"] == 404

    found = await client.get(f"/api/v1/patient-calls/{call.call_id}", params={"project_id": str(PROJECT_B)})
    assert found.json()["data"]["call_id"] == call.call_id


async def test_viewer_can_view_but_not_create_or_delete(app, client):
    patient = await make_patient(PROJECT_A)
    call = await make_patient_call(patient.patient_id)
    app.dependency_overrides[get_current_identity] = lambda: make_identity("VIEWER", project_ids=[str(PROJECT_A)])

    view_resp = await client.get("/api/v1/patient-calls", params={"project_id": str(PROJECT_A)})
    assert view_resp.json()["status"] == "success"

    delete_resp = await client.delete(f"/api/v1/patient-calls/{call.call_id}", params={"project_id": str(PROJECT_A)})
    assert delete_resp.json()["code"] == 403
