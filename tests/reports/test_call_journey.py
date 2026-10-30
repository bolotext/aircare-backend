"""GET /calls/{conversation_id}/journey (calls/journey.py): sibling calls are
linked by phone number server-side, ordered by time, annotated with the
retry-scheduler state, and inconsistencies (duplicate sequence_number, early
trigger, ...) are flagged. The phone number itself must never be returned.
"""

import pytest
from beanie import PydanticObjectId
from mongomock_motor import AsyncMongoMockClient

from reports_service.calls.schema_adapters import normalize_call_logs
from reports_service.core.deps import ProjectCallSource, get_current_identity, resolve_project_source
from tests.reports.factories import make_identity

pytestmark = pytest.mark.asyncio

PROJECT = PydanticObjectId()
ORG = PydanticObjectId()
PHONE = 7674912234


def _doc(cid, start, seq, mapping_day, triggered, next_trigger, **extra) -> dict:
    day = start[:10]
    doc = {
        "conversation_id": cid,
        "call_uid": cid,
        "flow_id": "prod-x",
        "call_status": "NO ANSWER",
        "callStartTime": f"ISODate({start.replace(' ', 'T')}+00:00)",
        "tempDate": f"{day[8:]}/{day[5:7]}/{day[:4]}",
        "phone_number": PHONE,
        "customerPhone": str(PHONE),
        "phone": f"+910{PHONE}",
        "sequence_number": seq,
        "trigger_call": True,
        "next_trigger_date": next_trigger,
        "call_sequence_mapping": {mapping_day: {"calls_triggered": triggered, "max_trigger": 3, "answered": 0, "not_answered": triggered}},
    }
    doc.update(extra)
    return doc


async def _journey(app, client, docs, conversation_id):
    collection = AsyncMongoMockClient()["aircare"]["call_logs"]
    await collection.insert_many(docs)
    app.dependency_overrides[resolve_project_source] = lambda: ProjectCallSource(
        collection=collection, organization_id=str(ORG), adapter=normalize_call_logs
    )
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")
    resp = await client.get(f"/api/v1/calls/{conversation_id}/journey", params={"project_id": str(PROJECT)})
    return resp.json()


async def test_journey_orders_calls_and_flags_duplicate_sequence_and_early_trigger(app, client):
    docs = [
        _doc("c3", "2026-10-27 04:31:55", 4, "2026-10-27", 1, "2026-10-27T07:30:01"),
        _doc("c1", "2026-10-26 08:27:17", 2, "2026-10-26", 1, "2026-10-26T10:27:46"),
        _doc("c2", "2026-10-27 03:56:39", 4, "2026-10-27", 1, "2026-10-27T06:53:59"),
        _doc("other", "2026-10-27 05:00:00", 1, "2026-10-27", 1, None, phone_number=9999999999, customerPhone="9999999999", phone="+919999999999"),
    ]
    body = await _journey(app, client, docs, "c1")

    assert body["code"] == 200
    data = body["data"]
    assert data["available"] is True
    assert [e["conversation_id"] for e in data["entries"]] == ["c1", "c2", "c3"]
    assert data["summary"]["total_calls"] == 3
    assert data["phone_masked"] == "******2234"

    c1, c2, c3 = data["entries"]
    assert c1["issues"] == []
    assert "duplicate_sequence_number" in {i["code"] for i in c2["issues"]}
    codes3 = {i["code"] for i in c3["issues"]}
    assert {"duplicate_sequence_number", "early_trigger", "mapping_count_mismatch"} <= codes3
    assert c3["scheduled_for"] == "2026-10-27T06:53:59"
    assert c3["seconds_vs_schedule"] < 0


async def test_journey_never_returns_the_phone_number(app, client):
    body = await _journey(app, client, [_doc("c1", "2026-10-26 08:27:17", 1, "2026-10-26", 1, None)], "c1")
    text = str(body)
    assert "7674912234" not in text


async def test_journey_unavailable_without_a_phone_number(app, client):
    doc = _doc("c1", "2026-10-26 08:27:17", 1, "2026-10-26", 1, None)
    for k in ("phone_number", "customerPhone", "phone"):
        doc.pop(k)
    body = await _journey(app, client, [doc], "c1")
    assert body["data"]["available"] is False
    assert body["data"]["entries"] == []


async def test_journey_404s_for_unknown_call(app, client):
    body = await _journey(app, client, [_doc("c1", "2026-10-26 08:27:17", 1, "2026-10-26", 1, None)], "nope")
    assert body["code"] == 404
