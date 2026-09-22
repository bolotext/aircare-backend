"""Exercises calls/schema_adapters.py::resolve_adapter directly — the
detection logic itself, as opposed to test_report_llm_schema_adapter.py,
which exercises normalize_report_llm through the API with the adapter
supplied by hand. No project/db/collection name is hardcoded anywhere here:
each case proves detection works from the *shape* of a sampled document
alone, which is the whole point of replacing the old (db, collection) ->
adapter registry.
"""

from datetime import datetime, timezone

import pytest
from mongomock_motor import AsyncMongoMockClient

from reports_service.calls import schema_adapters
from reports_service.calls.schema_adapters import (
    UnrecognizedReportSchemaError,
    normalize_aircare_calls,
    normalize_report_llm,
    resolve_adapter,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clear_adapter_cache():
    schema_adapters._detected_cache.clear()
    yield
    schema_adapters._detected_cache.clear()


async def test_detects_report_llm_shape_from_string_timestamp():
    collection = AsyncMongoMockClient()["detect_db"]["report_llm_shape"]
    await collection.insert_one({"callStartTime": "2025/02/13 14:56:32 +0000", "call_uid": "1"})

    assert await resolve_adapter(collection) is normalize_report_llm


async def test_detects_report_llm_shape_from_temp_date_fallback():
    collection = AsyncMongoMockClient()["detect_db"]["report_llm_tempdate"]
    # No callStartTime at all — only report_llm's YYYY-MM-DD tempDate.
    await collection.insert_one({"tempDate": "2025-02-13", "call_uid": "1"})

    assert await resolve_adapter(collection) is normalize_report_llm


async def test_detects_aircare_calls_shape_from_native_datetime():
    collection = AsyncMongoMockClient()["detect_db"]["aircare_calls_shape"]
    await collection.insert_one(
        {"callStartTime": datetime(2025, 2, 13, tzinfo=timezone.utc), "conversation_id": "1"}
    )

    assert await resolve_adapter(collection) is normalize_aircare_calls


async def test_detects_aircare_calls_shape_from_temp_date_fallback():
    collection = AsyncMongoMockClient()["detect_db"]["aircare_calls_tempdate"]
    # No callStartTime at all — only aircare_calls's DD/MM/YYYY tempDate.
    await collection.insert_one({"tempDate": "27/02/2025", "flow_id": "muthoot_predue_vb_tam"})

    assert await resolve_adapter(collection) is normalize_aircare_calls


async def test_native_call_shape_needs_no_adapter():
    collection = AsyncMongoMockClient()["detect_db"]["native_calls"]
    await collection.insert_one({"call_start_time": datetime.now(timezone.utc), "conversation_id": "1"})

    assert await resolve_adapter(collection) is None


async def test_empty_collection_returns_none_and_is_not_cached():
    collection = AsyncMongoMockClient()["detect_db"]["empty_then_populated"]

    assert await resolve_adapter(collection) is None

    # Once a newly connected, previously-empty project actually gets data,
    # the very next request must detect it — not stay stuck on the "empty"
    # guess forever.
    await collection.insert_one({"callStartTime": "2025/02/13 14:56:32 +0000", "call_uid": "1"})
    assert await resolve_adapter(collection) is normalize_report_llm


async def test_unrecognized_shape_raises_with_actionable_message():
    collection = AsyncMongoMockClient()["detect_db"]["unrecognized"]
    await collection.insert_one({"totally_unknown_field": 1, "another_one": "x"})

    with pytest.raises(UnrecognizedReportSchemaError, match="detect_db.unrecognized"):
        await resolve_adapter(collection)


async def test_detection_result_is_cached_per_collection():
    collection = AsyncMongoMockClient()["detect_db"]["cached"]
    await collection.insert_one({"callStartTime": "2025/02/13 14:56:32 +0000", "call_uid": "1"})

    first = await resolve_adapter(collection)
    await collection.delete_many({})

    # A once-detected, already-populated collection keeps its adapter even
    # if sampled again later when momentarily empty (e.g. a mid-batch
    # reload) — detection doesn't re-run per request.
    second = await resolve_adapter(collection)
    assert first is second is normalize_report_llm
