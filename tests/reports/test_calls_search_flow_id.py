"""`search` on GET /calls matches conversation_id OR flow_id (Conversation Logs
search box), on both the native-Call path and the raw-source adapter path."""

import pytest
from beanie import PydanticObjectId
from mongomock_motor import AsyncMongoMockClient

from reports_service.calls.schema_adapters import normalize_call_logs
from reports_service.core.deps import ProjectCallSource, get_current_identity, resolve_project_source
from tests.reports.factories import make_call, make_identity

pytestmark = pytest.mark.asyncio

PROJECT = PydanticObjectId()
ORG = PydanticObjectId()


def _ids(resp) -> list[str]:
    return sorted(i["conversation_id"] for i in resp.json()["data"]["items"])


async def test_search_matches_flow_id_on_native_calls(app, client):
    await make_call(PROJECT, ORG, conversation_id="a-1", bot_id="prod-aaa111")
    await make_call(PROJECT, ORG, conversation_id="b-2", bot_id="prod-bbb222")
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    by_flow = await client.get("/api/v1/calls", params={"project_id": str(PROJECT), "search": "PROD-bbb"})
    by_id = await client.get("/api/v1/calls", params={"project_id": str(PROJECT), "search": "a-1"})

    assert _ids(by_flow) == ["b-2"]
    assert _ids(by_id) == ["a-1"]


async def test_search_matches_flow_id_on_raw_source_documents(app, client):
    collection = AsyncMongoMockClient()["aircare"]["call_logs"]
    base = {
        "call_status": "ANSWERED",
        "callStartTime": "ISODate(2026-09-25T09:37:55+00:00)",
        "tempDate": "25/09/2026",
    }
    await collection.insert_many(
        [
            {**base, "_id": 1, "conversation_id": "c-1", "call_uid": "c-1", "flow_id": "prod-aaa111"},
            {**base, "_id": 2, "conversation_id": "c-2", "call_uid": "c-2", "flow_id": "prod-bbb222"},
        ]
    )
    app.dependency_overrides[resolve_project_source] = lambda: ProjectCallSource(
        collection=collection, organization_id=str(ORG), adapter=normalize_call_logs
    )
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    resp = await client.get("/api/v1/calls", params={"project_id": str(PROJECT), "search": "bbb222"})

    assert _ids(resp) == ["c-2"]
