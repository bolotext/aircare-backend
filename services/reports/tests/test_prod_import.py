"""POST /calls/import-from-prod (calls/prod_import.py): saves the prod record
into the chosen stage db only on a 200 answer, is Admin-only, needs the PII
confirmation, and never echoes the record back.
"""

import httpx
import pytest
from mongomock_motor import AsyncMongoMockClient

from reports_service.core.deps import get_current_identity
from tests.factories import make_identity

pytestmark = pytest.mark.asyncio

BODY = {
    "conversation_id": "1793075514226712234",
    "flow_id": "prod-e3fbb647899d",
    "db_name": "stage_db",
    "collection_name": "call_logs",
    "confirm_pii": True,
}
RECORD = {"conversation_data": {"full_name": "Test Person", "phone_number": 7674912234}, "customer_details": {}}


@pytest.fixture
def mongo(monkeypatch):
    client = AsyncMongoMockClient()
    monkeypatch.setattr("reports_service.calls.router.get_client", lambda: client)
    return client


def _prod_api(monkeypatch, status=200, json_body=RECORD):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["flow_id"] = request.headers.get("flow_id")
        return httpx.Response(status, json=json_body)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        "reports_service.calls.prod_import.httpx.AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    monkeypatch.setattr(
        "reports_service.calls.prod_import.get_settings",
        lambda: type("S", (), {"prod_import_url": "https://prod.example/mongo_test_conversation", "prod_import_timeout_seconds": 5})(),
    )
    return seen


async def test_saves_to_chosen_db_on_200_and_does_not_echo_the_record(app, client, mongo, monkeypatch):
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")
    seen = _prod_api(monkeypatch)

    resp = await client.post("/api/v1/calls/import-from-prod", json=BODY)
    body = resp.json()

    assert body["code"] == 200
    assert body["data"]["created"] is True
    assert "Test Person" not in resp.text and "7674912234" not in resp.text.replace(BODY["conversation_id"], "")
    assert seen["flow_id"] == "prod-e3fbb647899d"
    assert "q1=1793075514226712234" in seen["url"]

    saved = await mongo["stage_db"]["call_logs"].find_one({})
    assert saved["conversation_data"]["full_name"] == "Test Person"
    assert saved["_import_meta"]["source"] == "prod"

    again = (await client.post("/api/v1/calls/import-from-prod", json=BODY)).json()
    assert again["data"]["created"] is False
    assert await mongo["stage_db"]["call_logs"].count_documents({}) == 1


async def test_nothing_saved_when_prod_api_is_not_200(app, client, mongo, monkeypatch):
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")
    _prod_api(monkeypatch, status=500, json_body={"error": "boom"})

    body = (await client.post("/api/v1/calls/import-from-prod", json=BODY)).json()

    assert body["code"] == 502
    assert await mongo["stage_db"]["call_logs"].count_documents({}) == 0


async def test_rejects_reserved_db_and_missing_confirmation(app, client, mongo, monkeypatch):
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")
    _prod_api(monkeypatch)

    reserved = (await client.post("/api/v1/calls/import-from-prod", json={**BODY, "db_name": "admin"})).json()
    unconfirmed = (await client.post("/api/v1/calls/import-from-prod", json={**BODY, "confirm_pii": False})).json()

    assert reserved["code"] == 422
    assert unconfirmed["code"] == 422


async def test_supervisor_cannot_import(app, client, mongo, monkeypatch):
    app.dependency_overrides[get_current_identity] = lambda: make_identity("SUPERVISOR")
    _prod_api(monkeypatch)

    body = (await client.post("/api/v1/calls/import-from-prod", json=BODY)).json()

    assert body["code"] == 403
