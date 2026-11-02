from datetime import datetime, timezone

import pytest
from beanie import PydanticObjectId

from reports_service.core.deps import get_current_identity
from tests.reports.factories import make_call, make_identity

pytestmark = pytest.mark.asyncio

PROJECT = PydanticObjectId()
ORG = PydanticObjectId()


def _at(hour: int) -> datetime:
    return datetime(2026, 10, 20, hour, 5, tzinfo=timezone.utc)


async def _seed():
    await make_call(PROJECT, ORG, conversation_id="s-1", call_status="NO_ANSWER", disposition="ABANDONED", call_start_time=_at(9), connected_seconds=0)
    await make_call(
        PROJECT, ORG, conversation_id="s-2", call_status="ANSWERED", disposition="RESOLVED",
        call_start_time=_at(9), call_connected_time=_at(9), connected_seconds=40,
    )
    await make_call(
        PROJECT, ORG, conversation_id="s-3", call_status="ANSWERED", disposition="ABANDONED",
        call_start_time=_at(14), call_connected_time=_at(14), connected_seconds=5,
    )


async def test_collection_stats_funnel_hourly_and_buckets(app, client):
    await _seed()
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    resp = await client.get("/api/v1/dashboard/collection", params={"project_id": str(PROJECT)})
    body = resp.json()["data"]

    assert [s["count"] for s in body["funnel"]] == [3, 2, 1, 1]
    assert body["funnel"][0]["pct_of_dialled"] == 100
    assert body["trend"] == [{"date": "2026-10-20", "attempts": 3, "connected": 2}]
    assert {h["hour"]: (h["total"], h["connected"]) for h in body["hourly"]} == {9: (2, 1), 14: (1, 1)}
    assert {b["label"]: b["count"] for b in body["duration_buckets"]} == {"0-15 sec": 2, "16-60 sec": 1, "60+ sec": 0}


async def test_calls_filter_by_status_and_search(app, client):
    await _seed()
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    resp = await client.get("/api/v1/calls", params={"project_id": str(PROJECT), "call_status": "NO_ANSWER"})
    assert [i["conversation_id"] for i in resp.json()["data"]["items"]] == ["s-1"]

    resp = await client.get("/api/v1/calls", params={"project_id": str(PROJECT), "search": "S-3"})
    assert [i["conversation_id"] for i in resp.json()["data"]["items"]] == ["s-3"]


async def test_calls_list_returns_field_mapper_columns_and_withholds_pii(app, client):
    from reports_service.calls.model import Call
    from reports_service.core.deps import ProjectCallSource, resolve_project_source

    await _seed()
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")
    app.dependency_overrides[resolve_project_source] = lambda: ProjectCallSource(
        collection=Call.get_pymongo_collection(),
        organization_id=str(ORG),
        adapter=None,
        field_mapping={"lang": "language", "phone_number": "customerPhone", "stage": "no_such_field"},
    )

    resp = await client.get("/api/v1/calls", params={"project_id": str(PROJECT), "search": "s-2"})
    body = resp.json()["data"]

    assert body["columns"] == [
        {"key": "lang", "raw_field": "language", "restricted": False},
        {"key": "phone_number", "raw_field": "customerPhone", "restricted": False},
        {"key": "stage", "raw_field": "no_such_field", "restricted": False},
    ]
    assert body["items"][0]["fields"] == {"lang": "en-IN", "phone_number": None, "stage": None}


async def test_call_detail_returns_full_source_document_unmasked(app, client):
    from reports_service.calls.model import Call

    call = await make_call(PROJECT, ORG, conversation_id="detail-1")
    await Call.get_pymongo_collection().update_one(
        {"conversation_id": "detail-1"},
        {"$set": {"customerPhone": "+910000000000", "stage": "S1", "extra": {"a": [1, 2]}}},
    )
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    resp = await client.get("/api/v1/calls/detail-1", params={"project_id": str(PROJECT)})
    doc = resp.json()["data"]["source_document"]

    assert doc["conversation_id"] == "detail-1"
    assert doc["stage"] == "S1"
    assert doc["extra"] == {"a": [1, 2]}
    assert doc["customerPhone"] != "[withheld: personal data]"
    assert doc["customerPhone"] == "+910000000000"


async def test_bulk_source_documents_in_requested_order(app, client):
    await make_call(PROJECT, ORG, conversation_id="b-1")
    await make_call(PROJECT, ORG, conversation_id="b-2")
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    resp = await client.post(
        "/api/v1/calls/source-documents",
        params={"project_id": str(PROJECT)},
        json={"conversation_ids": ["b-2", "missing", "b-1"]},
    )
    docs = resp.json()["data"]

    assert [d["conversation_id"] for d in docs] == ["b-2", "b-1"]
