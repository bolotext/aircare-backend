"""POST /calls/import-from-prod (calls/prod_import.py): looks the customer up by
phone number on the prod report + customer APIs (flow_id from the project's
Data Source settings), saves calls to the project's report collection and the
customer record to its customer-details collection only on 200 answers, is
Admin-only, needs the PII confirmation, and never echoes the data back.
"""

import httpx
import pytest
from mongomock_motor import AsyncMongoMockClient

from reports_service.core.deps import get_current_identity
from reports_service.core.users_client import ProjectConfig
from tests.reports.factories import make_identity

pytestmark = pytest.mark.asyncio

PROJECT_ID = "6aae5deae9a48c4dc62aa398"
PHONE = "7674912234"
BODY = {"project_id": PROJECT_ID, "phone_number": PHONE, "confirm_pii": True}
CALLS = {
    "count": 2,
    "records": [
        {"conversation_id": "1793346039463212234", "full_name": "Test Person", "flow_id": "prod-e3fbb647899d"},
        {"conversation_id": "1793338655571012234", "full_name": "Test Person", "flow_id": "prod-e3fbb647899d"},
    ],
}
CUSTOMER = {"full_name": "Test Person", "phone_number": 7674912234, "sequence_number": 11}


@pytest.fixture
def mongo(monkeypatch):
    client = AsyncMongoMockClient()
    monkeypatch.setattr("reports_service.calls.router.get_client", lambda: client)
    return client


def _project(monkeypatch, app, flow_id="prod-e3fbb647899d", db="stage_db", collection="call_logs", customers="customers"):
    app.state.users_http_client = None  # lifespan doesn't run in tests; the config lookup is faked below

    async def fake_config(_client, project_id):
        return ProjectConfig(
            project_id=project_id,
            organization_id="org",
            report_db_name=db,
            report_collection_name=collection,
            customer_details_collection_name=customers,
            flow_id=flow_id,
        )

    monkeypatch.setattr("reports_service.calls.router.get_project_config", fake_config)


def _prod_api(monkeypatch, report=(200, CALLS), customer=(200, CUSTOMER)):
    seen = {"urls": [], "flow_ids": set()}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["urls"].append(str(request.url))
        seen["flow_ids"].add(request.headers.get("flow_id"))
        status, body = report if "mongo_test_report" in request.url.path else customer
        return httpx.Response(status, json=body)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        "reports_service.calls.prod_import.httpx.AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    monkeypatch.setattr(
        "reports_service.calls.prod_import.get_settings",
        lambda: type(
            "S",
            (),
            {
                "prod_import_base_url": "https://prod.example/",
                "prod_import_timeout_seconds": 5,
            },
        )(),
    )
    return seen


async def test_saves_calls_and_customer_and_does_not_echo_them(app, client, mongo, monkeypatch):
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")
    _project(monkeypatch, app)
    seen = _prod_api(monkeypatch)

    resp = await client.post("/api/v1/calls/import-from-prod", json=BODY)
    body = resp.json()

    assert body["code"] == 200
    assert body["data"]["calls_created"] == 2 and body["data"]["customer_saved"] is True
    assert "Test Person" not in resp.text
    assert seen["flow_ids"] == {"prod-e3fbb647899d"}
    assert all(f"number={PHONE}" in u for u in seen["urls"])

    assert await mongo["stage_db"]["call_logs"].count_documents({}) == 2
    saved = await mongo["stage_db"]["call_logs"].find_one({"conversation_id": "1793346039463212234"})
    assert saved["_import_meta"]["source"] == "prod"
    customer = await mongo["stage_db"]["customers"].find_one({})
    assert customer["sequence_number"] == 11 and customer["_import_meta"]["phone_number"] == PHONE

    again = (await client.post("/api/v1/calls/import-from-prod", json=BODY)).json()
    assert again["data"]["calls_created"] == 0 and again["data"]["calls_replaced"] == 2
    assert await mongo["stage_db"]["call_logs"].count_documents({}) == 2
    assert await mongo["stage_db"]["customers"].count_documents({}) == 1


async def test_customer_failure_keeps_calls_and_is_reported(app, client, mongo, monkeypatch):
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")
    _project(monkeypatch, app)
    _prod_api(monkeypatch, customer=(500, {"error": "boom"}))

    body = (await client.post("/api/v1/calls/import-from-prod", json=BODY)).json()

    assert body["code"] == 200
    assert body["data"]["customer_saved"] is False and "500" in body["data"]["customer_error"]
    assert await mongo["stage_db"]["call_logs"].count_documents({}) == 2
    assert await mongo["stage_db"]["customers"].count_documents({}) == 0


async def test_customer_skipped_without_customer_collection(app, client, mongo, monkeypatch):
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")
    _project(monkeypatch, app, customers=None)
    seen = _prod_api(monkeypatch)

    body = (await client.post("/api/v1/calls/import-from-prod", json=BODY)).json()

    assert body["data"]["customer_saved"] is None
    assert not any("mongo_test_customer" in u for u in seen["urls"])


@pytest.mark.parametrize(
    "report, code",
    [((500, {"error": "boom"}), 502), ((200, {"count": 0, "records": []}), 422)],
)
async def test_nothing_saved_when_report_api_is_not_200_or_empty(app, client, mongo, monkeypatch, report, code):
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")
    _project(monkeypatch, app)
    _prod_api(monkeypatch, report=report)

    body = (await client.post("/api/v1/calls/import-from-prod", json=BODY)).json()

    assert body["code"] == code
    assert await mongo["stage_db"]["call_logs"].count_documents({}) == 0
    assert await mongo["stage_db"]["customers"].count_documents({}) == 0


async def test_rejects_reserved_db_missing_flow_id_bad_phone_and_missing_confirmation(app, client, mongo, monkeypatch):
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")
    _prod_api(monkeypatch)

    _project(monkeypatch, app, db="admin")
    reserved = (await client.post("/api/v1/calls/import-from-prod", json=BODY)).json()
    _project(monkeypatch, app, flow_id=None)
    no_flow = (await client.post("/api/v1/calls/import-from-prod", json=BODY)).json()
    unconfirmed = (await client.post("/api/v1/calls/import-from-prod", json={**BODY, "confirm_pii": False})).json()
    bad_phone = (await client.post("/api/v1/calls/import-from-prod", json={**BODY, "phone_number": "12ab"})).json()

    assert reserved["code"] == 422
    assert no_flow["code"] == 422 and "Flow ID" in no_flow["message"]
    assert unconfirmed["code"] == 422 and bad_phone["code"] == 422


async def test_supervisor_cannot_import(app, client, mongo, monkeypatch):
    app.dependency_overrides[get_current_identity] = lambda: make_identity("SUPERVISOR")
    _prod_api(monkeypatch)

    body = (await client.post("/api/v1/calls/import-from-prod", json=BODY)).json()

    assert body["code"] == 403


async def test_preview_returns_full_responses_without_saving_then_import_uses_edited_payload(app, client, mongo, monkeypatch):
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")
    _project(monkeypatch, app)
    _prod_api(monkeypatch)

    prev = (await client.post("/api/v1/calls/import-from-prod/preview", json=BODY)).json()
    assert prev["code"] == 200
    assert prev["data"]["calls_response"] == CALLS and prev["data"]["customer_response"] == CUSTOMER
    assert prev["data"]["db_name"] == "stage_db" and prev["data"]["customer_collection_name"] == "customers"
    assert await mongo["stage_db"]["call_logs"].count_documents({}) == 0  # nothing saved by a preview

    edited = {**CALLS, "records": [{**CALLS["records"][0], "full_name": "Edited Name"}]}
    saved = (await client.post("/api/v1/calls/import-from-prod", json={**BODY, "calls_payload": edited, "customer_payload": CUSTOMER})).json()
    assert saved["data"]["calls_created"] == 1
    doc = await mongo["stage_db"]["call_logs"].find_one({})
    assert doc["full_name"] == "Edited Name"

    unconfirmed = (await client.post("/api/v1/calls/import-from-prod/preview", json={**BODY, "confirm_pii": False})).json()
    assert unconfirmed["code"] == 422


async def test_full_data_returns_both_responses_only_when_both_are_200(app, client, mongo, monkeypatch):
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")
    _project(monkeypatch, app)
    seen = _prod_api(monkeypatch)
    full_body = {"project_id": PROJECT_ID, "phone_number": PHONE}
    ok = (await client.post("/api/v1/calls/prod-full-data", json=full_body)).json()
    assert ok["code"] == 200 and ok["data"]["calls_response"] == CALLS and ok["data"]["customer_response"] == CUSTOMER
    assert any("mongo_test_customer" in u for u in seen["urls"]) and any("mongo_test_report" in u for u in seen["urls"])
    assert await mongo["stage_db"]["call_logs"].count_documents({}) == 0
    assert (await client.post("/api/v1/calls/prod-full-data", json={**full_body, "phone_number": "12ab"})).json()["code"] == 422


async def test_full_data_fails_when_customer_api_is_not_200(app, client, monkeypatch):
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")
    _project(monkeypatch, app)
    _prod_api(monkeypatch, customer=(404, {"error": "none"}))
    assert (await client.post("/api/v1/calls/prod-full-data", json={"project_id": PROJECT_ID, "phone_number": PHONE})).json()["code"] == 502
