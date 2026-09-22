from datetime import datetime, timedelta, timezone

import pytest
from beanie import PydanticObjectId

from reports_service.calls import repository as call_repo
from reports_service.calls.model import Call

pytestmark = pytest.mark.asyncio

PROJECT = PydanticObjectId()
ORG = PydanticObjectId()


def _payload(**overrides) -> dict:
    now = datetime.now(timezone.utc)
    payload = {
        "conversation_id": "cid-1",
        "project_id": str(PROJECT),
        "organization_id": str(ORG),
        "bot_id": "bot",
        "language": "en-IN",
        "call_status": "ANSWERED",
        "disposition": "RESOLVED",
        "call_start_time": now,
        "call_connected_time": now,
        "call_end_time": now,
        "connected_seconds": 60.0,
        "tool_failure_count": 0,
        "raw": {},
    }
    payload.update(overrides)
    return payload


async def test_duplicate_write_produces_exactly_one_document(app):
    await call_repo.upsert_call_from_payload(_payload())
    await call_repo.upsert_call_from_payload(_payload())  # exact replay, same conversation_id

    all_calls = await Call.find(Call.conversation_id == "cid-1").to_list()
    assert len(all_calls) == 1


async def test_out_of_order_callback_does_not_clobber_newer_state(app):
    t0 = datetime.now(timezone.utc)
    t1 = t0 + timedelta(seconds=5)

    # Newer event arrives first (e.g. a final "completed" callback).
    await call_repo.upsert_call_from_payload(_payload(disposition="RESOLVED", event_time=t1))
    # Older/duplicated event replays after — must NOT overwrite the newer disposition.
    await call_repo.upsert_call_from_payload(_payload(disposition="NEUTRAL", event_time=t0))

    call = await call_repo.get_by_conversation_id("cid-1", str(PROJECT))
    assert call.disposition == "RESOLVED"

    all_calls = await Call.find(Call.conversation_id == "cid-1").to_list()
    assert len(all_calls) == 1


async def test_newer_event_after_older_updates_state(app):
    t0 = datetime.now(timezone.utc)
    t1 = t0 + timedelta(seconds=5)

    await call_repo.upsert_call_from_payload(_payload(disposition="NEUTRAL", event_time=t0))
    await call_repo.upsert_call_from_payload(_payload(disposition="RESOLVED", event_time=t1))

    call = await call_repo.get_by_conversation_id("cid-1", str(PROJECT))
    assert call.disposition == "RESOLVED"
