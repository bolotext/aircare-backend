"""Exercises calls/schema_adapters.py::normalize_call_logs end to end through
the /calls, /calls/{id}, and /dashboard routes. Regression coverage for the
production bug where Demo Project A's configured collection
(`aircare.call_logs`) 422'd every request: its `callStartTime`/
`callConnectedTime`/`callEndTime` are the literal string
"ISODate(2026-09-25T09:37:55+00:00)", which matched neither
`_is_report_llm_shape` nor `_is_aircare_calls_shape`, so `resolve_adapter`
raised `UnrecognizedReportSchemaError` for every request against it. Also
verifies this source's health-condition/location fields
(`condition`/`city`/`user_context`) never reach the API response, same as
the other adapters' phone/name/recording PII.
"""

import pytest
from beanie import PydanticObjectId
from mongomock_motor import AsyncMongoMockClient

from reports_service.core.deps import ProjectCallSource, get_current_identity, resolve_project_source
from reports_service.calls.schema_adapters import normalize_call_logs
from tests.factories import make_identity

pytestmark = pytest.mark.asyncio

PROJECT = PydanticObjectId()
ORG = PydanticObjectId()


def _raw_call_logs_doc(**overrides) -> dict:
    doc = {
        "_id": "raw-1",
        "conversation_id": "1790329075540158485",
        "call_uid": "1790329075540158485",
        "bot_id": "23d97519803144b199bb1986b67e7c78",
        "flow_id": "prod-b6117f2226cd",
        "language": "english",
        "resolved_language": "hindi",
        "call_status": "ANSWERED",
        "callStatus": None,
        "STAGE_CODE": "APPOINTMENT_INTERESTED",
        "DISPOSITION": "APPOINTMENT_INTERESTED",
        "callStartTime": "ISODate(2026-09-25T09:37:55+00:00)",
        "callConnectedTime": "ISODate(2026-09-25T09:37:59+00:00)",
        "callEndTime": "ISODate(2026-09-25T09:38:53+00:00)",
        "tempDate": "25/09/2026",
        "gnanivb_billing_duration": "54",
        "gnanivb_dial_status": "ANSWER",
        "gnanivb_hangup_cause_code": "16",
        "gnanivb_hangup_cause_text_tech": "Normal Clearing",
        "user_name": "narendra.singh@gnani.ai",
        "phone": "+9107389058485",
        "phone_number": 7389058485,
        "srcPhone": "+918035218872",
        "dstPhone": "7389058485|prod-b6117f2226cd;;Narendra Singh",
        "rec_path": "prod-b6117f2226cd/2026_09_25/call.wav",
        "conversation_log": [{"role": "user", "content": "confidential patient detail"}],
        "patient_id": "PT-0003",
        "condition": "copd",
        "city": "Bengaluru",
        "user_context": {"phone": "+9107389058485", "condition": "copd", "city": "Bengaluru"},
    }
    doc.update(overrides)
    return doc


def _override_call_logs_source(app, collection) -> None:
    app.dependency_overrides[resolve_project_source] = lambda: ProjectCallSource(
        collection=collection, organization_id=str(ORG), adapter=normalize_call_logs
    )


async def test_list_calls_normalizes_call_logs_documents_and_omits_pii(app, client):
    collection = AsyncMongoMockClient()["aircare"]["call_logs"]
    await collection.insert_one(_raw_call_logs_doc())

    _override_call_logs_source(app, collection)
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    resp = await client.get("/api/v1/calls", params={"project_id": str(PROJECT)})
    body = resp.json()["data"]
    assert body["total"] == 1
    item = body["items"][0]

    assert item["conversation_id"] == "1790329075540158485"
    assert item["project_id"] == str(PROJECT)
    assert item["bot_id"] == "23d97519803144b199bb1986b67e7c78"
    assert item["call_status"] == "ANSWERED"
    assert item["disposition"] == "APPOINTMENT_INTERESTED"
    assert item["connected_seconds"] == 54.0

    payload_text = resp.text
    for pii in (
        "narendra.singh@gnani.ai",
        "9107389058485",
        "8035218872",
        "rec_path",
        "confidential patient detail",
        "copd",
        "Bengaluru",
        "PT-0003",
    ):
        assert pii not in payload_text


async def test_call_detail_omits_pii_and_health_data_from_raw(app, client):
    collection = AsyncMongoMockClient()["aircare"]["call_logs"]
    await collection.insert_one(_raw_call_logs_doc())

    _override_call_logs_source(app, collection)
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    resp = await client.get("/api/v1/calls/1790329075540158485", params={"project_id": str(PROJECT)})
    data = resp.json()["data"]
    assert data["organization_id"] == str(ORG)
    for key in ("phone", "rec_path", "conversation_log", "condition", "city", "patient_id", "user_context"):
        assert key not in data["raw"]
    assert data["raw"]["hangup_cause_code"] == "16"


async def test_dashboard_metrics_computed_in_python_for_call_logs(app, client):
    collection = AsyncMongoMockClient()["aircare"]["call_logs"]
    await collection.insert_one(_raw_call_logs_doc(_id="raw-1", call_uid="answered-1"))
    await collection.insert_one(
        _raw_call_logs_doc(
            _id="raw-2",
            call_uid="unanswered-1",
            call_status="NO_ANSWER",
            callConnectedTime=None,
            callEndTime=None,
            gnanivb_billing_duration=None,
            gnanivb_dial_status="NOANSWER",
        )
    )

    _override_call_logs_source(app, collection)
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    resp = await client.get("/api/v1/dashboard", params={"project_id": str(PROJECT)})
    metrics = resp.json()["data"]
    assert metrics["total_calls"] == 2
    assert metrics["answered_calls"] == 1
    assert metrics["answer_rate_pct"] == 50.0
