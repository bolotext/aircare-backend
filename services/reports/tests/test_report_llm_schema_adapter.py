"""Exercises calls/schema_adapters.py::normalize_report_llm end to end
through the /calls, /calls/{id}, and /dashboard routes — the actual point
being (1) a project whose configured collection is real call-center CDR
data, not this service's own Call schema, still gets filtered/paginated/
aggregated correctly, and (2) the PII that source schema carries (name,
phone numbers, recording path) never reaches the API response.
`conversation_log` is the one exception (see schema_adapters.py's module
docstring): it's deliberately surfaced in `raw` for the call-detail
transcript view.
"""

import pytest
from beanie import PydanticObjectId
from mongomock_motor import AsyncMongoMockClient

from reports_service.core.deps import ProjectCallSource, get_current_identity, resolve_project_source
from reports_service.calls.schema_adapters import normalize_report_llm
from tests.factories import make_identity

pytestmark = pytest.mark.asyncio

PROJECT = PydanticObjectId()
ORG = PydanticObjectId()


def _raw_report_llm_doc(**overrides) -> dict:
    doc = {
        "_id": "raw-1",
        "name": "manoj",
        "phone": 8553079962,
        "phone_number": 8553079962,
        "customerPhone": "8553079962",
        "srcPhone": "+917313119575",
        "dstPhone": "8553079962|balic-llm-en;;manoj",
        "flow_id": "balic-llm-en",
        "call_uid": "1739438565473667214",
        "tempDate": "2025-02-13",
        "call_status": None,
        "callStatus": "ANSWERED",
        "callStartTime": "2025/02/13 14:56:32 +0000",
        "callConnectedTime": "2025/02/13 14:56:35 +0000",
        "callEndTime": "2025/02/13 14:57:04 +0000",
        "gnanivb_billing_duration": "28",
        "gnanivb_dial_status": "ANSWER",
        "gnanivb_hangup_cause_code": "16",
        "gnanivb_hangup_cause_text_tech": "Normal Clearing",
        "stage": "initial_message",
        "sequence_number": 1,
        "answered_seq": 1,
        "no_answer_seq": 0,
        "rec_path": "balic-llm-en/2025_02_13/call.wav",
        "conversation_log": [{"asr_text": "hello", "bot_response": "some confidential policy detail"}],
        "redis_data": "empty",
    }
    doc.update(overrides)
    return doc


def _override_report_llm_source(app, collection) -> None:
    app.dependency_overrides[resolve_project_source] = lambda: ProjectCallSource(
        collection=collection, organization_id=str(ORG), adapter=normalize_report_llm
    )


async def test_list_calls_normalizes_report_llm_documents_and_omits_pii(app, client):
    collection = AsyncMongoMockClient()["demo_collection"]["report_llm"]
    await collection.insert_one(_raw_report_llm_doc())

    _override_report_llm_source(app, collection)
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    resp = await client.get("/api/v1/calls", params={"project_id": str(PROJECT)})
    body = resp.json()["data"]
    assert body["total"] == 1
    item = body["items"][0]

    assert item["conversation_id"] == "1739438565473667214"
    assert item["project_id"] == str(PROJECT)
    assert item["bot_id"] == "balic-llm-en"
    assert item["call_status"] == "ANSWERED"
    assert item["connected_seconds"] == 28.0

    # The paginated list view never includes `raw` at all (see
    # calls/schema.py::CallListItem) — conversation_log only ever surfaces
    # via GET /calls/{conversation_id} (see the detail test below).
    payload_text = resp.text
    for pii in ("manoj", "8553079962", "917313119575", "rec_path", "confidential policy detail"):
        assert pii not in payload_text


async def test_call_detail_omits_pii_from_raw_but_keeps_conversation_log(app, client):
    collection = AsyncMongoMockClient()["demo_collection"]["report_llm"]
    await collection.insert_one(_raw_report_llm_doc())

    _override_report_llm_source(app, collection)
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    resp = await client.get("/api/v1/calls/1739438565473667214", params={"project_id": str(PROJECT)})
    data = resp.json()["data"]
    assert data["organization_id"] == str(ORG)
    assert "name" not in data["raw"]
    assert "phone" not in data["raw"]
    assert "rec_path" not in data["raw"]
    assert data["raw"]["hangup_cause_code"] == "16"
    assert data["raw"]["conversation_log"] == [
        {"asr_text": "hello", "bot_response": "some confidential policy detail"}
    ]


async def test_dashboard_metrics_computed_in_python_for_report_llm(app, client):
    collection = AsyncMongoMockClient()["demo_collection"]["report_llm"]
    await collection.insert_one(_raw_report_llm_doc(_id="raw-1", call_uid="answered-1"))
    await collection.insert_one(
        _raw_report_llm_doc(
            _id="raw-2",
            call_uid="unanswered-1",
            callStatus="NO_ANSWER",
            callConnectedTime=None,
            callEndTime=None,
            gnanivb_billing_duration=None,
            gnanivb_dial_status="NOANSWER",
        )
    )

    _override_report_llm_source(app, collection)
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    resp = await client.get("/api/v1/dashboard", params={"project_id": str(PROJECT)})
    metrics = resp.json()["data"]
    assert metrics["total_calls"] == 2
    assert metrics["answered_calls"] == 1
    assert metrics["answer_rate_pct"] == 50.0


async def test_date_filter_applies_to_report_llm_calendar_date(app, client):
    collection = AsyncMongoMockClient()["demo_collection"]["report_llm"]
    await collection.insert_one(_raw_report_llm_doc(_id="raw-1", call_uid="in-range", tempDate="2025-02-13"))
    await collection.insert_one(
        _raw_report_llm_doc(
            _id="raw-2",
            call_uid="out-of-range",
            tempDate="2025-03-01",
            callStartTime="2025/03/01 10:00:00 +0000",
            callConnectedTime="2025/03/01 10:00:05 +0000",
            callEndTime="2025/03/01 10:01:00 +0000",
        )
    )

    _override_report_llm_source(app, collection)
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    resp = await client.get(
        "/api/v1/calls", params={"project_id": str(PROJECT), "date_from": "2025-02-13", "date_to": "2025-02-13"}
    )
    body = resp.json()["data"]
    assert body["total"] == 1
    assert body["items"][0]["conversation_id"] == "in-range"


async def test_scenario_edit_is_written_to_the_raw_report_document(app, client):
    collection = AsyncMongoMockClient()["demo_collection"]["report_llm"]
    await collection.insert_one(_raw_report_llm_doc())
    _override_report_llm_source(app, collection)
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")
    params = {"project_id": str(PROJECT)}

    saved = await client.put("/api/v1/calls/1739438565473667214/scenario", params=params, json={"scenario": "Valid PTP date"})
    assert saved.json()["code"] == 200

    raw = await collection.find_one({"_id": "raw-1"})
    assert raw["scenarios"] == "Valid PTP date"
    assert raw["scenarios_updated_by"] == "000000000000000000000001"
    # the raw document's own fields are untouched
    assert raw["flow_id"] == "balic-llm-en" and raw["customerPhone"] == "8553079962"

    listed = await client.get("/api/v1/calls", params=params)
    assert listed.json()["data"]["items"][0]["scenario"] == "Valid PTP date"
    assert "8553079962" not in listed.text
