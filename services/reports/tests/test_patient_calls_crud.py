import pytest
from beanie import PydanticObjectId

from reports_service.core.deps import get_current_identity
from tests.factories import make_identity, make_patient, make_patient_call

pytestmark = pytest.mark.asyncio

PROJECT = PydanticObjectId()

_LIVE_READING = {
    "value": 180.0,
    "timestamp": "2026-09-21T15:30:00Z",
    "raw_response": {"pm25_concentration_ugm3": 90.0},
}


async def test_create_list_get_update_delete_round_trip(app, client):
    patient = await make_patient(PROJECT)
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    create_resp = await client.post(
        "/api/v1/patient-calls",
        json={
            "call_id": "ext-call-abc123",
            "patient_id": patient.patient_id,
            "project_id": str(PROJECT),
            "phone_number": patient.phone_number,
            "city": patient.city,
            "condition": patient.condition,
            "direction": "outbound",
            "live_aqi_reading": _LIVE_READING,
            "risk_threshold": 150,
            "reading_timestamp": "2026-09-21T15:30:00Z",
            "status": "APPOINTMENT_INTERESTED",
        },
    )
    created = create_resp.json()["data"]
    assert created["call_id"] == "ext-call-abc123"
    assert created["patient_id"] == patient.patient_id
    assert created["second_aqi_reading"] is None

    list_resp = await client.get("/api/v1/patient-calls", params={"project_id": str(PROJECT)})
    list_body = list_resp.json()["data"]
    assert list_body["total"] == 1
    assert list_body["items"][0]["call_id"] == "ext-call-abc123"

    get_resp = await client.get(f"/api/v1/patient-calls/ext-call-abc123", params={"project_id": str(PROJECT)})
    assert get_resp.json()["data"]["status"] == "APPOINTMENT_INTERESTED"

    update_resp = await client.patch(
        "/api/v1/patient-calls/ext-call-abc123",
        params={"project_id": str(PROJECT)},
        json={
            "status": "APPOINTMENT_BOOKED",
            "telephony_triggered": True,
            "second_aqi_reading": {"value": 53, "timestamp": "2026-06-23T10:00:00+05:30", "raw_response": {}},
            "aqi_reconciliation_delta": 4.3,
        },
    )
    updated = update_resp.json()["data"]
    assert updated["status"] == "APPOINTMENT_BOOKED"
    assert updated["telephony_triggered"] is True
    assert updated["second_aqi_reading"]["value"] == 53
    assert updated["aqi_reconciliation_delta"] == 4.3
    assert updated["patient_id"] == patient.patient_id  # untouched fields survive a partial update

    delete_resp = await client.delete("/api/v1/patient-calls/ext-call-abc123", params={"project_id": str(PROJECT)})
    assert delete_resp.json()["data"] == {"deleted": True}

    missing_resp = await client.get("/api/v1/patient-calls/ext-call-abc123", params={"project_id": str(PROJECT)})
    assert missing_resp.json()["code"] == 404


async def test_create_requires_patient_to_exist_in_project(app, client):
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    resp = await client.post(
        "/api/v1/patient-calls",
        json={
            "call_id": "ext-call-no-patient",
            "patient_id": "PT-DOES-NOT-EXIST",
            "project_id": str(PROJECT),
            "phone_number": "+910000000000",
            "city": "Delhi",
            "condition": "asthma",
            "direction": "outbound",
            "live_aqi_reading": _LIVE_READING,
            "risk_threshold": 150,
            "reading_timestamp": "2026-09-21T15:30:00Z",
            "status": "APPOINTMENT_INTERESTED",
        },
    )
    assert resp.json()["code"] == 404


async def test_duplicate_call_id_is_conflict(app, client):
    patient = await make_patient(PROJECT)
    await make_patient_call(patient.patient_id, call_id="dup-call")
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    resp = await client.post(
        "/api/v1/patient-calls",
        json={
            "call_id": "dup-call",
            "patient_id": patient.patient_id,
            "project_id": str(PROJECT),
            "phone_number": patient.phone_number,
            "city": patient.city,
            "condition": patient.condition,
            "direction": "outbound",
            "live_aqi_reading": _LIVE_READING,
            "risk_threshold": 150,
            "reading_timestamp": "2026-09-21T15:30:00Z",
            "status": "APPOINTMENT_INTERESTED",
        },
    )
    assert resp.json()["code"] == 409


async def test_list_filters_by_patient_id(app, client):
    patient_a = await make_patient(PROJECT)
    patient_b = await make_patient(PROJECT)
    await make_patient_call(patient_a.patient_id)
    await make_patient_call(patient_b.patient_id)
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    resp = await client.get(
        "/api/v1/patient-calls", params={"project_id": str(PROJECT), "patient_id": patient_a.patient_id}
    )
    body = resp.json()["data"]
    assert body["total"] == 1
    assert body["items"][0]["patient_id"] == patient_a.patient_id
