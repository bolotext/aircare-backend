from datetime import datetime, timedelta, timezone

import pytest
from beanie import PydanticObjectId

from reports_service.core.deps import get_current_identity
from tests.factories import make_call, make_identity

pytestmark = pytest.mark.asyncio

PROJECT = PydanticObjectId()
ORG = PydanticObjectId()


async def test_pagination_and_total(app, client):
    for i in range(5):
        await make_call(PROJECT, ORG, conversation_id=f"c-{i}")

    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    resp = await client.get("/api/v1/calls", params={"project_id": str(PROJECT), "page": 1, "page_size": 2})
    body = resp.json()["data"]
    assert body["total"] == 5
    assert len(body["items"]) == 2
    assert body["page"] == 1
    assert body["page_size"] == 2


async def test_filter_by_language_and_disposition(app, client):
    await make_call(PROJECT, ORG, conversation_id="a", language="hi-IN", disposition="RESOLVED")
    await make_call(PROJECT, ORG, conversation_id="b", language="en-IN", disposition="ABANDONED")

    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    resp = await client.get("/api/v1/calls", params={"project_id": str(PROJECT), "language": "hi-IN"})
    items = resp.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["conversation_id"] == "a"

    resp = await client.get("/api/v1/calls", params={"project_id": str(PROJECT), "disposition": "ABANDONED"})
    items = resp.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["conversation_id"] == "b"


async def test_filter_by_date_range(app, client):
    now = datetime.now(timezone.utc)
    await make_call(PROJECT, ORG, conversation_id="old", call_start_time=now - timedelta(days=10))
    await make_call(PROJECT, ORG, conversation_id="recent", call_start_time=now)

    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    # date_from/date_to are date-only (YYYY-MM-DD), not full ISO datetimes.
    resp = await client.get(
        "/api/v1/calls",
        params={"project_id": str(PROJECT), "date_from": (now - timedelta(days=1)).date().isoformat()},
    )
    items = resp.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["conversation_id"] == "recent"


async def test_date_to_includes_the_entire_calendar_day(app, client):
    # A call late in the day on date_to's date must still be included:
    # date_to is the end of that calendar day, not midnight at its start.
    today = datetime.now(timezone.utc).date()
    late_in_day = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=23, minutes=59)
    await make_call(PROJECT, ORG, conversation_id="late-today", call_start_time=late_in_day)
    await make_call(PROJECT, ORG, conversation_id="tomorrow", call_start_time=late_in_day + timedelta(hours=1))

    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    resp = await client.get(
        "/api/v1/calls",
        params={"project_id": str(PROJECT), "date_from": today.isoformat(), "date_to": today.isoformat()},
    )
    items = resp.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["conversation_id"] == "late-today"


async def test_call_detail_includes_raw_transcript(app, client):
    await make_call(
        PROJECT, ORG, conversation_id="detail-1", raw={"conversation_log": [{"role": "assistant", "content": "hi"}]}
    )

    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    resp = await client.get("/api/v1/calls/detail-1", params={"project_id": str(PROJECT)})
    data = resp.json()["data"]
    assert data["raw"]["conversation_log"][0]["content"] == "hi"
