import base64
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Annotated

from dateutil import parser as dateutil_parser
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from mongo_mcp_client import MongoMCPError, mcp_find, mcp_insert
from redis_mcp_client import (
    RedisMCPError,
    redis_lpop,
    redis_lrange,
    redis_rpush,
)

from .schema import (
    BookingErrorResponse,
    NextSlotRequest,
    NextSlotResponse,
    ReserveSlotRequest,
    ReserveSlotResponse,
    VerifyIdentityRequest,
    VerifyIdentityResponse,
)

logger = logging.getLogger("booking_router")

router = APIRouter(prefix="/mcp", tags=["booking"])

# ---------------------------------------------------------------------------
# Identity verification + teleconsultation slot booking
#
# Routes below are declared under this router's own /mcp prefix; app.py
# mounts it at /api/v1, giving /api/v1/mcp/<route> so the API Gateway's proxy
# (which always forwards /api/v1/<first-path-segment>/...) can reach it via
# the "mcp" segment — see api-gateway's app.py::_route_map.
#
# Two tools the voice agent calls mid-call:
#   verify_identity(call_id, patient_id, spoken_dob) -> {"verified", "reason"}
#   reserve_slot(candidate_slot_id) -> {"result", "slot_id", ...}
# plus GET /mcp/next_slot(date), a non-destructive peek the agent uses to
# learn every slot_id available on a given date so it can offer the patient
# a choice before calling reserve_slot with whichever slot_id was picked.
# Neither next_slot nor reserve_slot takes a call_id/patient_id — next_slot
# has no state to scope by them, and reserve_slot's lack of them means it's
# not idempotent and its audit record can't be linked back to a specific
# call or patient (see its docstring and the Response contract note below).
#
# Both tools talk to Mongo only through mcp_find/mcp_insert and to Redis only
# through redis_rpush/redis_lpop/redis_lrange
# (mongo_mcp_client.py, redis_mcp_client.py, siblings of this booking/
# package) — never a direct driver.
#
# Slot storage model: slots:available is a Redis list of JSON-encoded
# {slot_id, doctor_name, date_time} records, oldest-first (rpush appends,
# lpop claims from the head). Claiming the exact slot_id the patient picked
# (not just whatever's at the head) needs _claim_slot's lpop/rpush scan below,
# since the Redis MCP toolset has no LREM — see its docstring for the
# concurrency tradeoff that's an acceptable one for this demo/seed scaffolding
# (_ensure_slots_seeded generates half-hour slots across the next several
# days, starting tomorrow) standing in for a real scheduling system's
# availability feed.
#
# Response contract
# ------------------
# POST /mcp/verify_identity -> 200 always: {"verified": bool, "reason": str | None}.
#   reason is free text, not a fixed enum — "not_found" | "mismatch" |
#   "lookup_failed" on failure, a next-step hint (e.g. "Continue to
#   teleconsultation slot booking") on success. Never raises on a failed
#   verification — only a 422 on missing/invalid call_id/patient_id/spoken_dob,
#   since "identity didn't verify" is an expected, not an error, outcome.
# GET /mcp/next_slot -> 200: {"available": bool, "slots": [{"slot_id", "date_time",
#   "doctor_name"}, ...]}. `slots` holds every available slot on the
#   requested `date` (query param), earliest-first; empty (and unparseable
#   dates fall through to this too) when unavailable. Non-destructive — never
#   pops the queue.
# POST /mcp/reserve_slot -> 200: {"result": "reserved" | "reoffered" | "no_slot",
#   "slot_id", "date_time", "doctor_name"}. `candidate_slot_id` can be any
#   slot_id /next_slot just returned, not only the first — "reserved" means
#   that exact slot was claimed; "reoffered" means it couldn't be found
#   (already taken, or stale) and a different real slot was claimed instead.
#   NOT idempotent: takes no call_id, so a replay (e.g. a retried request)
#   claims a second, different slot rather than returning the first
#   reservation, and its call_events audit doc can't be linked back to a
#   specific call or patient.
# 422 FastAPI's standard validation-error body on missing/invalid required
#   fields, on either endpoint (bodies/query params are typed Pydantic models
#   from schema.py — see VerifyIdentityRequest, NextSlotRequest,
#   ReserveSlotRequest).
# 502 {"error": "mongo_mcp_failure" | "redis_mcp_failure"} on a genuine MCP
#   transport failure — never silently swallowed as a business-logic outcome.
# ---------------------------------------------------------------------------

SLOTS_QUEUE_KEY = "slots:available:v2"  # v2: bumped from "slots:available" when seeding moved from a single day to _DEMO_SLOT_DAYS_AHEAD days, so any pre-existing single-day queue in Redis is abandoned instead of being mistaken for "not stale yet" and served forever
_DEMO_SLOT_DOCTORS = ["Dr. Rao", "Dr. Iyer", "Dr. Khan", "Dr. Mehta"]
_DEMO_SLOT_COUNT_PER_DAY = 12
_DEMO_SLOT_SPACING_MINUTES = 30
_DEMO_SLOT_DAYS_AHEAD = 7  # next_slot can be asked about any date within this window (tomorrow .. tomorrow + N-1)
SLOT_LIST_MAX_COUNT = _DEMO_SLOT_COUNT_PER_DAY * _DEMO_SLOT_DAYS_AHEAD  # reserve_slot's claim search window below — must cover every seeded slot, so reserve_slot can always find whatever /next_slot showed

_slots_seed_lock = threading.Lock()


def _iso_now():
    return datetime.now(timezone.utc).isoformat()


def _im_error(status_code, error, reason, **extra):
    body = {"error": error, "reason": reason}
    body.update(extra)
    return JSONResponse(status_code=status_code, content=body)


def _b64_json_encode(obj):
    """Base64-wrap a JSON payload before handing it to the Redis MCP server's
    rpush/set tools. Their `value` parameter is typed as a Union (redis-py's
    EncodableT: bytes/bytearray/memoryview/str/int/float), not a bare str, so
    the MCP SDK's own argument pre-parser (func_metadata.py::pre_parse_json,
    on the redis-mcp-server side) opportunistically json.loads()s any string
    argument for a non-str-only-typed parameter — turning our JSON *object*
    string back into a dict before the tool's own Pydantic validation runs,
    which then rejects it (a dict matches none of that Union's members). This
    was the "6 validation errors for rpushArguments ... input_type=dict"
    502 seen from /next_slot and /reserve_slot. Base64 output is never valid
    JSON, so that pre-parse attempt raises, is swallowed, and our string
    passes through untouched."""
    return base64.b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")


def _b64_json_decode(raw):
    """Reverse _b64_json_encode. Falls back to plain json.loads for a value
    this code never wrote through the encode side above — e.g. slots:available
    populated directly by a real external scheduling feed (see
    _ensure_slots_seeded's docstring), which never round-trips through our
    own rpush call and so is never base64-wrapped."""
    try:
        return json.loads(base64.b64decode(raw, validate=True).decode("utf-8"))
    except (ValueError, TypeError):
        return json.loads(raw)


def _slot_is_future(raw):
    slot_time = _slot_date_time(_b64_json_decode(raw))
    if slot_time is None:
        return False  # corrupt entry — treat as stale so it gets flushed
    return slot_time > datetime.now(timezone.utc)


def _ensure_slots_seeded():
    """Demo/seed scaffolding only: a real deployment would have slots:available
    populated by an actual doctor-scheduling system, not generated here.
    /next_slot is a non-destructive peek, so slots left unclaimed just sit in
    the queue — checked on every call (not just when the queue is empty) so a
    fully-populated queue of now-past slots doesn't get served indefinitely;
    re-seeds whenever the head of the queue has already passed. Seeds
    _DEMO_SLOT_DAYS_AHEAD days' worth of slots (not just tomorrow), so
    /next_slot can be asked about any date in that window, not only the
    very next day."""
    with _slots_seed_lock:
        head = redis_lrange(SLOTS_QUEUE_KEY, 0, 0)
        if head and _slot_is_future(head[0]):
            return
        while redis_lpop(SLOTS_QUEUE_KEY) is not None:
            pass  # drain any stale/corrupt leftovers before reseeding
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=4, minute=0, second=0, microsecond=0)
        for day_offset in range(_DEMO_SLOT_DAYS_AHEAD):
            day_base = tomorrow + timedelta(days=day_offset)
            for i in range(_DEMO_SLOT_COUNT_PER_DAY):
                slot_time = day_base + timedelta(minutes=_DEMO_SLOT_SPACING_MINUTES * i)
                slot = {
                    "slot_id": f"SLOT-{slot_time.strftime('%Y%m%dT%H%MZ')}",
                    "doctor_name": _DEMO_SLOT_DOCTORS[i % len(_DEMO_SLOT_DOCTORS)],
                    "date_time": slot_time.isoformat(),
                }
                redis_rpush(SLOTS_QUEUE_KEY, _b64_json_encode(slot))


def _parse_date(value):
    """Parse a spoken or typed date into a plain date for comparison; None
    (never a guess) if it can't be parsed at all. Shared by dob verification
    and slot-date matching — both just need "what calendar date is this"."""
    if not value:
        return None
    try:
        return dateutil_parser.parse(str(value), fuzzy=True).date()
    except (ValueError, TypeError, OverflowError):
        return None


def _slot_date_time(slot):
    """Parse a decoded slot's date_time back into an aware datetime; None if
    the entry is corrupt (missing/unparseable date_time)."""
    try:
        return datetime.fromisoformat(slot["date_time"])
    except (ValueError, TypeError, KeyError):
        return None


@router.post("/verify_identity", response_model=VerifyIdentityResponse)
async def verify_identity(body: VerifyIdentityRequest):
    call_id, patient_id, spoken_dob = body.call_id, body.patient_id, body.spoken_dob
    logger.warning(f"verify_identity request: call_id={call_id} patient_id={patient_id}, spoken_dob={spoken_dob}")

    try:
        patients = mcp_find("patients", {"patient_id": patient_id}, limit=1, call_id=call_id)
    except MongoMCPError:
        logger.exception(f"verify_identity: Mongo MCP failure call_id={call_id}")
        return {"verified": False, "reason": "Looking up patient record failed — please try again or contact support"}

    patient = patients[0] if patients else None
    if not patient:
        return {"verified": False, "reason": "Patient not found — please try again or contact support"}

    stored_dob = _parse_date(patient.get("dob"))
    spoken_dob_parsed = _parse_date(spoken_dob)
    if stored_dob is None:
        logger.warning(f"verify_identity: patient_id={patient_id} has no usable dob on record")
        return {"verified": False, "reason": "Looking up patient record failed — please try again or contact support"}
    if spoken_dob_parsed is None or spoken_dob_parsed != stored_dob:
        return {"verified": False, "reason": "DOB mismatch — please try again or contact support"}
    return {"verified": True, "reason": "Continue to teleconsultation slot booking"}


@router.get("/next_slot", response_model=NextSlotResponse, responses={502: {"model": BookingErrorResponse}})
def next_slot(params: Annotated[NextSlotRequest, Query()]):
    date = params.date
    logger.warning(f"next_slot request: date={date}")

    target_date = _parse_date(date)
    if target_date is None:
        logger.warning(f"next_slot: unparseable date={date!r}")
        return {"available": False, "slots": []}

    try:
        _ensure_slots_seeded()
        peeked = redis_lrange(SLOTS_QUEUE_KEY, 0, -1)
    except RedisMCPError as e:
        logger.exception("next_slot: Redis MCP failure")
        return _im_error(502, "redis_mcp_failure", str(e))

    slots = [_b64_json_decode(raw) for raw in peeked]
    day_slots = [s for s in slots if (t := _slot_date_time(s)) is not None and t.date() == target_date]
    return {"available": bool(day_slots), "slots": day_slots}


def _claim_slot(candidate_slot_id):
    """Best-effort claim of a *specific* slot_id out of slots:available, for
    when the caller picked something other than the very first slot
    /next_slot showed. Built from lpop/rpush since the Redis MCP toolset has
    no LREM (see redis_mcp_client.py's module docstring for the exact
    primitives exposed): pops one record at a time, up to SLOT_LIST_MAX_COUNT
    of them — enough to cover every seeded slot — immediately rpushing back
    anything that doesn't match. Not atomic across concurrent reserve_slot
    calls, and a miss re-queues the skipped slots behind whatever it never
    touched rather than preserving their original position — acceptable for
    this demo/seed scaffold, where no slot is ever lost or double-booked,
    only reordered. Returns the claimed slot dict, or None if
    candidate_slot_id wasn't found in the window (already taken, or
    stale)."""
    for _ in range(SLOT_LIST_MAX_COUNT):
        raw = redis_lpop(SLOTS_QUEUE_KEY)
        if raw is None:
            return None
        slot = _b64_json_decode(raw)
        if slot.get("slot_id") == candidate_slot_id:
            return slot
        redis_rpush(SLOTS_QUEUE_KEY, raw)
    return None


@router.post("/reserve_slot", response_model=ReserveSlotResponse, responses={502: {"model": BookingErrorResponse}})
async def reserve_slot(body: ReserveSlotRequest):
    candidate_slot_id = body.candidate_slot_id
    logger.warning(f"reserve_slot request: candidate_slot_id={candidate_slot_id}")

    try:
        _ensure_slots_seeded()
        claimed = _claim_slot(candidate_slot_id)
        reoffered = None
        if claimed is None:
            reoffered_raw = redis_lpop(SLOTS_QUEUE_KEY)
            reoffered = _b64_json_decode(reoffered_raw) if reoffered_raw else None
    except RedisMCPError as e:
        logger.exception("reserve_slot: Redis MCP failure")
        return _im_error(502, "redis_mcp_failure", str(e))

    if claimed is not None:
        result = {"result": "reserved", "slot_id": claimed["slot_id"], "date_time": claimed["date_time"], "doctor_name": claimed["doctor_name"]}
    elif reoffered is not None:
        result = {"result": "reoffered", "slot_id": reoffered["slot_id"], "date_time": reoffered["date_time"], "doctor_name": reoffered["doctor_name"]}
    else:
        result = {"result": "no_slot", "slot_id": None, "date_time": None, "doctor_name": None}
    _persist_booking_event(result)
    return result


def _persist_booking_event(result):
    """Best-effort audit record — no call_id/patient_id to key or dedupe on
    (see reserve_slot's docstring note in the module header), so a retried
    insert after an ambiguous timeout may write a duplicate doc rather than
    being deduped."""
    try:
        mcp_insert(
            "call_events",
            {
                "event": "teleconsultation_booking",
                "result": result.get("result"), "slot_id": result.get("slot_id"),
                "date_time": result.get("date_time"), "doctor_name": result.get("doctor_name"),
                "created_at": _iso_now(),
            },
        )
    except MongoMCPError:
        logger.exception("reserve_slot: failed to persist booking audit doc")
