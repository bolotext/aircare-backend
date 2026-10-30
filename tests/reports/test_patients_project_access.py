"""Proves project isolation for the patient roster: 'if a user has access to
that project, only then can they see those patients' (the feature's own
requirement) — mirrors test_project_access.py's coverage for /calls.
"""

import pytest
from beanie import PydanticObjectId

from reports_service.core.deps import get_current_identity
from tests.reports.factories import make_identity, make_patient

pytestmark = pytest.mark.asyncio

PROJECT_A = PydanticObjectId()
PROJECT_B = PydanticObjectId()


async def test_missing_project_id_is_rejected(app, client):
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    resp = await client.get("/api/v1/patients")
    assert resp.json()["code"] == 422


async def test_supervisor_sees_only_their_assigned_projects_patients(app, client):
    await make_patient(PROJECT_A, full_name="In project A")
    await make_patient(PROJECT_B, full_name="In project B")

    app.dependency_overrides[get_current_identity] = lambda: make_identity("SUPERVISOR", project_ids=[str(PROJECT_A)])

    resp = await client.get("/api/v1/patients", params={"project_id": str(PROJECT_A)})
    body = resp.json()["data"]
    assert body["total"] == 1
    assert body["items"][0]["full_name"] == "In project A"

    denied = await client.get("/api/v1/patients", params={"project_id": str(PROJECT_B)})
    assert denied.json()["code"] == 403


async def test_viewer_can_view_but_not_create_or_delete(app, client):
    patient = await make_patient(PROJECT_A)
    app.dependency_overrides[get_current_identity] = lambda: make_identity("VIEWER", project_ids=[str(PROJECT_A)])

    view_resp = await client.get("/api/v1/patients", params={"project_id": str(PROJECT_A)})
    assert view_resp.json()["status"] == "success"

    create_resp = await client.post(
        "/api/v1/patients",
        json={
            "project_id": str(PROJECT_A), "full_name": "X", "phone_number": "1", "dob": "1990-01-01",
            "city": "Delhi", "condition": "asthma", "preferred_language": "hindi",
        },
    )
    assert create_resp.json()["code"] == 403

    delete_resp = await client.delete(f"/api/v1/patients/{patient.patient_id}", params={"project_id": str(PROJECT_A)})
    assert delete_resp.json()["code"] == 403


async def test_cross_project_patient_lookup_is_not_found_not_leaked(app, client):
    patient = await make_patient(PROJECT_B)

    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    resp = await client.get(f"/api/v1/patients/{patient.patient_id}", params={"project_id": str(PROJECT_A)})
    assert resp.json()["code"] == 404
