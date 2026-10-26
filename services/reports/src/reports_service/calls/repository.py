"""The single source of query/filter logic for calls. Every consumer — the
paginated list route, the dashboard metrics aggregation, the CSV export, and
(Phase 3) the automailer — builds its Mongo query through `build_mongo_query`
so "identical filter logic" is structurally true rather than coincidentally
true.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from beanie import PydanticObjectId
from motor.motor_asyncio import AsyncIOMotorCollection
from pymongo import ReturnDocument

from reports_service.calls.model import Call

@dataclass(frozen=True)
class CallFilter:
    # None = no project restriction (only valid for the "all projects"
    # combined automailer scope — see mailer.pipeline._scope_to_project_ids).
    project_ids: list[str] | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    language: str | None = None
    disposition: str | None = None
    call_status: str | None = None
    # Case-insensitive substring match on conversation_id.
    search: str | None = None


def day_range_bounds(date_from: date | None, date_to: date | None) -> tuple[datetime | None, datetime | None]:
    """Converts inclusive, calendar-day API query params (`YYYY-MM-DD`) into
    the `[start, end)` UTC instant bounds `build_mongo_query`'s `$gte`/`$lt`
    window expects: `date_from` at 00:00:00 UTC that day, `date_to` at
    00:00:00 UTC the *following* day (exclusive), so the entirety of
    `date_to`'s calendar day is included rather than cut off at midnight.
    """

    start = datetime.combine(date_from, time.min, tzinfo=timezone.utc) if date_from is not None else None
    end = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=timezone.utc) if date_to is not None else None
    return start, end


def _to_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def build_mongo_query(call_filter: CallFilter) -> dict:
    query: dict = {}
    if call_filter.project_ids is not None:
        query["project_id"] = {"$in": [PydanticObjectId(pid) for pid in call_filter.project_ids]}
    if call_filter.date_from is not None or call_filter.date_to is not None:
        window: dict = {}
        if call_filter.date_from is not None:
            window["$gte"] = call_filter.date_from
        if call_filter.date_to is not None:
            window["$lt"] = call_filter.date_to
        query["call_start_time"] = window
    if call_filter.language:
        query["language"] = call_filter.language
    if call_filter.disposition:
        query["disposition"] = call_filter.disposition
    if call_filter.call_status:
        query["call_status"] = call_filter.call_status
    if call_filter.search:
        query["conversation_id"] = {"$regex": re.escape(call_filter.search), "$options": "i"}
    return query


def _with_source(call: Call, doc: dict) -> Call:
    call._source_doc = doc
    return call


def _default_collection() -> AsyncIOMotorCollection:
    return Call.get_pymongo_collection()


async def fetch_normalized_calls(
    coll: AsyncIOMotorCollection,
    adapter: Callable[..., dict],
    project_id: str | None,
    organization_id: str,
) -> list[Call]:
    """Fetches every raw document from a non-native-schema collection and
    normalizes each into a (never-persisted) Call via `adapter` — see
    calls.schema_adapters. The whole collection is fetched rather than
    pushed down as a Mongo filter because the source schema's own field
    names/types (e.g. call timestamps stored as "YYYY/MM/DD HH:MM:SS +0000"
    strings, no `call_start_time`/`disposition`/`language` fields at all)
    don't match what build_mongo_query filters on; filtering, sorting and
    pagination for this path all happen in Python instead (see
    matches_filter and every caller below). Fine at this collection's
    current size; a genuinely large non-native source would need a real
    per-schema query translator instead.
    """

    raw_docs = await coll.find({}).to_list(None)
    return [_with_source(Call.model_validate(adapter(doc, project_id=project_id, organization_id=organization_id)), doc) for doc in raw_docs]


def matches_filter(call: Call, call_filter: CallFilter) -> bool:
    """The Python-side equivalent of build_mongo_query, applied to an
    already-normalized Call — used for the adapter path (see
    fetch_normalized_calls) in place of a pushed-down Mongo query. Deliberately
    does not check call.project_id: an adapted source collection carries no
    project_id field of its own (see calls.schema_adapters), so every
    project configured to read it sees the same underlying data.
    """

    if call_filter.date_from is not None and call.call_start_time < call_filter.date_from:
        return False
    if call_filter.date_to is not None and call.call_start_time >= call_filter.date_to:
        return False
    if call_filter.language and call.language != call_filter.language:
        return False
    if call_filter.disposition and call.disposition != call_filter.disposition:
        return False
    if call_filter.call_status and call.call_status != call_filter.call_status:
        return False
    if call_filter.search and call_filter.search.lower() not in call.conversation_id.lower():
        return False
    return True


async def list_paginated(
    call_filter: CallFilter,
    page: int,
    page_size: int,
    collection: AsyncIOMotorCollection | None = None,
    adapter: Callable[..., dict] | None = None,
    organization_id: str | None = None,
) -> tuple[list[Call], int]:
    """`collection` defaults to the fixed shared `calls` collection, but a
    caller that already resolved a project's own configured report source
    (see core.deps.resolve_project_source) passes that in instead — same
    query logic, different Mongo target, same connection. `adapter` (and the
    `organization_id` it needs — see calls.schema_adapters) switches to the
    Python-side filter/sort/paginate path in fetch_normalized_calls/matches_filter
    for a project whose configured collection isn't already in this
    service's own Call schema.
    """

    coll = collection if collection is not None else _default_collection()

    if adapter is not None:
        project_id = (call_filter.project_ids or [None])[0]
        calls = await fetch_normalized_calls(coll, adapter, project_id, organization_id or "")
        matched = sorted(
            (c for c in calls if matches_filter(c, call_filter)), key=lambda c: c.call_start_time, reverse=True
        )
        start = (page - 1) * page_size
        return matched[start : start + page_size], len(matched)

    query = build_mongo_query(call_filter)
    total = await coll.count_documents(query)
    cursor = coll.find(query).sort("call_start_time", -1).skip((page - 1) * page_size).limit(page_size)
    items = [_with_source(Call.model_validate(doc), doc) async for doc in cursor]
    return items, total


async def list_all(
    call_filter: CallFilter,
    collection: AsyncIOMotorCollection | None = None,
    adapter: Callable[..., dict] | None = None,
    organization_id: str | None = None,
) -> list[Call]:
    """Unpaginated fetch for the CSV export / automailer — a report needs
    every matching call, not one page of them. See list_paginated for what
    `adapter`/`organization_id` switch on.
    """

    coll = collection if collection is not None else _default_collection()

    if adapter is not None:
        project_id = (call_filter.project_ids or [None])[0]
        calls = await fetch_normalized_calls(coll, adapter, project_id, organization_id or "")
        return sorted((c for c in calls if matches_filter(c, call_filter)), key=lambda c: c.call_start_time, reverse=True)

    query = build_mongo_query(call_filter)
    cursor = coll.find(query).sort("call_start_time", -1)
    return [Call.model_validate(doc) async for doc in cursor]


async def get_by_conversation_id(
    conversation_id: str,
    project_id: str,
    collection: AsyncIOMotorCollection | None = None,
    adapter: Callable[..., dict] | None = None,
    organization_id: str | None = None,
) -> Call | None:
    """`project_id` is required (not just an access-control formality): a
    project's configured report collection may be shared with other projects
    (e.g. the demo fixtures all point at the same `aircare.calls`
    collection), so without this filter a caller with access to project A
    could fetch project B's call by conversation_id alone. See
    list_paginated for what `adapter`/`organization_id` switch on — the
    adapter path can't apply that same project_id filter (the source
    schema carries none), so it matches on conversation_id alone.
    """

    coll = collection if collection is not None else _default_collection()

    if adapter is not None:
        calls = await fetch_normalized_calls(coll, adapter, project_id, organization_id or "")
        return next((c for c in calls if c.conversation_id == conversation_id), None)

    doc = await coll.find_one({"conversation_id": conversation_id, "project_id": PydanticObjectId(project_id)})
    return _with_source(Call.model_validate(doc), doc) if doc is not None else None


async def get_many_by_conversation_ids(
    conversation_ids: list[str],
    project_id: str,
    collection: AsyncIOMotorCollection,
    adapter: Callable[..., dict] | None = None,
    organization_id: str | None = None,
) -> list[Call]:
    """The calls for `conversation_ids` within one project, in the order
    requested; ids that don't exist are skipped. Same project scoping rules
    as get_by_conversation_id, but reads the collection once rather than once
    per id (the adapter path loads the whole collection each time)."""

    wanted = set(conversation_ids)
    if adapter is not None:
        calls = await fetch_normalized_calls(collection, adapter, project_id, organization_id or "")
        found = {c.conversation_id: c for c in calls if c.conversation_id in wanted}
    else:
        cursor = collection.find({"conversation_id": {"$in": list(wanted)}, "project_id": PydanticObjectId(project_id)})
        found = {doc["conversation_id"]: _with_source(Call.model_validate(doc), doc) async for doc in cursor}
    return [found[cid] for cid in conversation_ids if cid in found]


async def get_with_raw_source_by_conversation_id(
    conversation_id: str,
    project_id: str,
    collection: AsyncIOMotorCollection,
    adapter: Callable[..., dict],
    organization_id: str | None = None,
) -> tuple[Call, dict] | None:
    """Adapter-path variant of get_by_conversation_id that also returns the
    original raw source document alongside the normalized Call — for a
    caller (see calls.recording) that needs a raw field the normalizer
    deliberately excludes from Call/its `raw` output, e.g. recording
    lookup's `flow_id`/`call_uid` (schema_adapters strips the `rec_path`
    field these reconstruct, as PII — see its module docstring). Only
    meaningful on the adapter path: a native-Call-schema document already
    IS its own "raw" representation (see get_by_conversation_id).
    """

    raw_docs = await collection.find({}).to_list(None)
    for doc in raw_docs:
        normalized = Call.model_validate(adapter(doc, project_id=project_id, organization_id=organization_id or ""))
        if normalized.conversation_id == conversation_id:
            return normalized, doc
    return None


async def upsert_call_from_payload(payload: dict) -> Call:
    """Idempotent, upsert-by-conversation_id write used both by the seed
    script and by the dedup/ordering tests standing in for a future live
    ingestion endpoint (see docs/KNOWN_LIMITATIONS.md).

    `payload` may include an optional `event_time` key: when present, an
    incoming write is only applied if it is newer than (or equal to) the
    stored `last_event_at`, so a duplicated or out-of-order callback replay
    can never clobber a document with newer data. When `event_time` is
    omitted (e.g. the seed script, which has no upstream event ordering to
    reason about), the write is plain last-write-wins.
    """

    conversation_id = payload["conversation_id"]
    event_time = payload.get("event_time")
    doc_fields = {k: v for k, v in payload.items() if k != "event_time"}
    # This writes through the raw collection (see below), bypassing Beanie's
    # own field coercion, so project_id/organization_id must be normalized to
    # ObjectId here — a stored BSON string would never match a
    # {"$in": [ObjectId(...)]} query built by build_mongo_query.
    for id_field in ("project_id", "organization_id"):
        if id_field in doc_fields and not isinstance(doc_fields[id_field], PydanticObjectId):
            doc_fields[id_field] = PydanticObjectId(doc_fields[id_field])

    collection = Call.get_pymongo_collection()
    existing = await collection.find_one({"conversation_id": conversation_id})

    if existing is not None and event_time is not None:
        existing_event_time = existing.get("last_event_at")
        # PyMongo returns naive UTC datetimes for BSON dates by default (no
        # tz_aware=True on the client), so a stored last_event_at compares
        # naive-vs-aware against a caller-supplied tz-aware event_time unless
        # both sides are normalized first.
        if existing_event_time is not None and _to_naive_utc(existing_event_time) >= _to_naive_utc(event_time):
            return await Call.get(existing["_id"])

    set_fields = dict(doc_fields)
    if event_time is not None:
        set_fields["last_event_at"] = event_time

    result = await collection.find_one_and_update(
        {"conversation_id": conversation_id},
        {
            "$set": set_fields,
            "$setOnInsert": {"created_at": datetime.now(timezone.utc)},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return await Call.get(result["_id"])
