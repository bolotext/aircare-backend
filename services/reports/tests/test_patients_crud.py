import pytest
from beanie import PydanticObjectId

from reports_service.core.deps import get_current_identity
from tests.factories import make_identity, make_patient

pytestmark = pytest.mark.asyncio

PROJECT = PydanticObjectId()


async def test_create_list_get_update_delete_round_trip(app, client):
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    create_resp = await client.post(
        "/api/v1/patients",
        json={
            "project_id": str(PROJECT),
            "full_name": "Jane Doe",
            "phone_number": "+911234567890",
            "dob": "1985-04-12",
            "city": "Delhi",
            "condition": "asthma",
            "preferred_language": "hindi",
            "risk_flag": True,
        },
    )
    created = create_resp.json()["data"]
    assert created["patient_id"].startswith("PT-")
    assert created["risk_flag"] is True
    patient_id = created["patient_id"]

    list_resp = await client.get("/api/v1/patients", params={"project_id": str(PROJECT)})
    list_body = list_resp.json()["data"]
    assert list_body["total"] == 1
    assert list_body["items"][0]["patient_id"] == patient_id

    get_resp = await client.get(f"/api/v1/patients/{patient_id}", params={"project_id": str(PROJECT)})
    assert get_resp.json()["data"]["full_name"] == "Jane Doe"

    update_resp = await client.patch(
        f"/api/v1/patients/{patient_id}", params={"project_id": str(PROJECT)}, json={"risk_flag": False, "city": "Mumbai"}
    )
    updated = update_resp.json()["data"]
    assert updated["risk_flag"] is False
    assert updated["city"] == "Mumbai"
    assert updated["full_name"] == "Jane Doe"  # untouched fields survive a partial update

    delete_resp = await client.delete(f"/api/v1/patients/{patient_id}", params={"project_id": str(PROJECT)})
    assert delete_resp.json()["data"] == {"deleted": True}

    missing_resp = await client.get(f"/api/v1/patients/{patient_id}", params={"project_id": str(PROJECT)})
    assert missing_resp.json()["code"] == 404


async def test_patient_ids_are_server_generated_and_unique():
    from datetime import date

    from reports_service.patients import repository as patient_repo

    p1 = await patient_repo.create_patient(
        project_id=str(PROJECT), full_name="A", phone_number="1", dob=date(1990, 1, 1), city="Delhi",
        condition="asthma", preferred_language="hindi",
    )
    p2 = await patient_repo.create_patient(
        project_id=str(PROJECT), full_name="B", phone_number="2", dob=date(1990, 1, 1), city="Delhi",
        condition="asthma", preferred_language="hindi",
    )
    assert p1.patient_id != p2.patient_id
    assert p1.patient_id.startswith("PT-")
    assert p2.patient_id.startswith("PT-")


async def test_list_filters_by_condition_and_risk_flag(app, client):
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")
    await make_patient(PROJECT, condition="asthma", risk_flag=True)
    await make_patient(PROJECT, condition="copd", risk_flag=False)

    resp = await client.get("/api/v1/patients", params={"project_id": str(PROJECT), "condition": "asthma"})
    body = resp.json()["data"]
    assert body["total"] == 1
    assert body["items"][0]["condition"] == "asthma"

    resp = await client.get("/api/v1/patients", params={"project_id": str(PROJECT), "risk_flag": "true"})
    body = resp.json()["data"]
    assert body["total"] == 1
    assert body["items"][0]["risk_flag"] is True
