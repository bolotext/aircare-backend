import pytest

from reports_service.core.deps import get_current_identity
from tests.reports.factories import make_identity, make_risk_threshold

pytestmark = pytest.mark.asyncio


async def test_supervisor_and_viewer_denied_on_every_route(app, client):
    threshold = await make_risk_threshold(condition="asthma")

    for role in ("SUPERVISOR", "VIEWER"):
        app.dependency_overrides[get_current_identity] = lambda role=role: make_identity(role, project_ids=[])

        assert (await client.get("/api/v1/risk-thresholds")).json()["code"] == 403
        assert (await client.get("/api/v1/risk-thresholds/asthma")).json()["code"] == 403
        assert (
            await client.post("/api/v1/risk-thresholds", json={"condition": "copd", "risk_threshold_aqi": 120, "pollutant": "pm2_5"})
        ).json()["code"] == 403
        assert (
            await client.patch("/api/v1/risk-thresholds/asthma", json={"risk_threshold_aqi": 160})
        ).json()["code"] == 403
        assert (await client.delete(f"/api/v1/risk-thresholds/{threshold.condition}")).json()["code"] == 403


async def test_admin_full_crud_round_trip(app, client):
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    create_resp = await client.post(
        "/api/v1/risk-thresholds", json={"condition": "bronchitis", "risk_threshold_aqi": 140, "pollutant": "pm2_5"}
    )
    assert create_resp.json()["data"]["condition"] == "bronchitis"

    list_resp = await client.get("/api/v1/risk-thresholds")
    assert len(list_resp.json()["data"]) == 1

    update_resp = await client.patch("/api/v1/risk-thresholds/bronchitis", json={"risk_threshold_aqi": 145})
    assert update_resp.json()["data"]["risk_threshold_aqi"] == 145

    delete_resp = await client.delete("/api/v1/risk-thresholds/bronchitis")
    assert delete_resp.json()["data"] == {"deleted": True}

    missing_resp = await client.get("/api/v1/risk-thresholds/bronchitis")
    assert missing_resp.json()["code"] == 404


async def test_duplicate_condition_is_conflict(app, client):
    await make_risk_threshold(condition="asthma")
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    resp = await client.post(
        "/api/v1/risk-thresholds", json={"condition": "asthma", "risk_threshold_aqi": 999, "pollutant": "pm2_5"}
    )
    assert resp.json()["code"] == 409
