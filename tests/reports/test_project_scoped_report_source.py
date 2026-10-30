"""Proves the actual point of core.deps.resolve_project_source: two
projects backed by *different* Mongo databases/collections (not just
different project_id values within one shared collection) are each read from
their own configured source, and never bleed into each other, across the
calls list, call detail, and dashboard endpoints.
"""

from datetime import datetime, timezone

import pytest
from beanie import PydanticObjectId
from mongomock_motor import AsyncMongoMockClient

from reports_service.calls.model import Call
from reports_service.core.deps import ProjectCallSource, get_current_identity, resolve_project_source
from tests.reports.factories import make_call, make_identity

pytestmark = pytest.mark.asyncio

PROJECT_A = PydanticObjectId()
PROJECT_B = PydanticObjectId()
ORG = PydanticObjectId()


def _make_other_project_collection():
    """Simulates a project whose report data lives in an entirely separate
    Mongo database/collection from the fixed shared one every other test
    fixture (make_call) writes to.
    """

    other_client = AsyncMongoMockClient()
    return other_client["other_project_db"]["other_project_calls"]


async def _insert_raw_call(collection, **overrides) -> None:
    now = datetime.now(timezone.utc)
    doc = {
        "project_id": PROJECT_B,
        "organization_id": ORG,
        "conversation_id": "b-1",
        "bot_id": "test-bot",
        "language": "en-IN",
        "call_status": "ANSWERED",
        "disposition": "RESOLVED",
        "call_start_time": now,
        "call_connected_time": now,
        "call_end_time": now,
        "connected_seconds": 45.0,
        "tool_failure_count": 0,
        "raw": {},
        "created_at": now,
    }
    doc.update(overrides)
    await collection.insert_one(doc)


def _override_per_project_collection(app, other_collection) -> None:
    async def _resolve(project_id: str):
        if project_id == str(PROJECT_B):
            return ProjectCallSource(collection=other_collection, organization_id=str(ORG), adapter=None)
        return ProjectCallSource(collection=Call.get_pymongo_collection(), organization_id=str(ORG), adapter=None)

    app.dependency_overrides[resolve_project_source] = _resolve


async def test_list_calls_reads_from_each_projects_own_collection(app, client):
    other_collection = _make_other_project_collection()
    await _insert_raw_call(other_collection)
    await make_call(PROJECT_A, ORG, conversation_id="a-1")

    _override_per_project_collection(app, other_collection)
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    resp_a = await client.get("/api/v1/calls", params={"project_id": str(PROJECT_A)})
    body_a = resp_a.json()["data"]
    assert body_a["total"] == 1
    assert body_a["items"][0]["conversation_id"] == "a-1"

    resp_b = await client.get("/api/v1/calls", params={"project_id": str(PROJECT_B)})
    body_b = resp_b.json()["data"]
    assert body_b["total"] == 1
    assert body_b["items"][0]["conversation_id"] == "b-1"


async def test_call_detail_only_found_in_its_own_projects_collection(app, client):
    other_collection = _make_other_project_collection()
    await _insert_raw_call(other_collection)

    _override_per_project_collection(app, other_collection)
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    found = await client.get("/api/v1/calls/b-1", params={"project_id": str(PROJECT_B)})
    assert found.json()["data"]["conversation_id"] == "b-1"

    # Same conversation_id, wrong project: project A's collection has no such
    # document, so this is a 404, not a cross-project data leak.
    not_found = await client.get("/api/v1/calls/b-1", params={"project_id": str(PROJECT_A)})
    assert not_found.json()["code"] == 404


async def test_dashboard_metrics_computed_from_the_projects_own_collection(app, client):
    other_collection = _make_other_project_collection()
    await _insert_raw_call(other_collection, disposition="RESOLVED")
    await _insert_raw_call(other_collection, conversation_id="b-2", disposition="ABANDONED", call_status="NO_ANSWER")

    _override_per_project_collection(app, other_collection)
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    resp = await client.get("/api/v1/dashboard", params={"project_id": str(PROJECT_B)})
    metrics = resp.json()["data"]
    assert metrics["total_calls"] == 2
    assert metrics["dispositions"] == {"RESOLVED": 1, "ABANDONED": 1}
