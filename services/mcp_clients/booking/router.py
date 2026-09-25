import base64
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Annotated

from dateutil import parser as dateutil_parser
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from mongo_mcp_client import MongoMCPError, mcp_find, mcp_insert
from redis_mcp_client import (
    RedisMCPError,
    redis_get,
    redis_llen,
    redis_lpop,
    redis_lrange,
    redis_rpush,
    redis_set,
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

router = APIRouter(tags=["booking"])

# ---------------------------------------------------------------------------
# Identity verification + teleconsultation slot booking
#
# Routes below are declared relative to this router (no prefix); app.py
# mounts it at /api/v1 so the API Gateway's proxy (which always forwards
# /api/v1/<first-path-segment>/...) can reach it — see
# api-gateway's app.py::_route_map.
#
# Two tools the voice agent calls mid-call:
#   verify_identity(call_id, patient_id, spoken_dob) -> {"verified", "reason"}
#   reserve_slot(call_id, patient_id, candidate_slot_id) -> {"result", "slot_id", ...}
# plus GET /next_slot, a non-destructive peek the agent uses to learn what
# candidate_slot_id(s) it can propose before calling reserve_slot — it can
# return more than one upcoming slot (see `count`) so the agent can offer the
# patient a choice, not just the single earliest one.
#
# Both tools talk to Mongo only through mcp_find/mcp_insert and to Redis only
# through redis_rpush/redis_lpop/redis_lrange/redis_llen/redis_get/redis_set
# (mongo_mcp_client.py, redis_mcp_client.py, siblings of this booking/
# package) — never a direct driver.
#
# Slot storage model: slots:available is a Redis list of JSON-encoded
# {slot_id, doctor_name, date_time} records, oldest-first (rpush appends,
# lpop claims from the head). Claiming the exact slot_id the patient picked
# (not just whatever's at the head) needs _claim_slot's lpop/rpush scan below,
# since the Redis MCP toolset has no LREM — see its docstring for the
# concurrency tradeoff that's an acceptable one for this demo/seed scaffolding
# (_ensure_slots_seeded generates a handful of half-hour slots starting
# tomorrow) standing in for a real scheduling system's availability feed.
#
# Response contract
# ------------------
# POST /verify_identity -> 200 always: {"verified": bool, "reason": str | None}.
#   reason is free text, not a fixed enum — "not_found" | "mismatch" |
#   "lookup_failed" on failure, a next-step hint (e.g. "Continue to
#   teleconsultation slot booking") on success. Never raises on a failed
#   verification — only a 422 on missing/invalid call_id/patient_id/spoken_dob,
#   since "identity didn't verify" is an expected, not an error, outcome.
# GET /next_slot -> 200: {"available": bool, "slots": [{"slot_id", "date_time",
#   "doctor_name"}, ...]}. `slots` is earliest-first and up to `count` long
#   (query param, default 3, max SLOT_LIST_MAX_COUNT=10); empty when
#   unavailable. Non-destructive — never pops the queue.
# POST /reserve_slot -> 200: {"result": "reserved" | "reoffered" | "no_slot",
#   "slot_id", "date_time", "doctor_name"}. `candidate_slot_id` can be any
#   slot_id /next_slot just returned, not only the first — "reserved" means
#   that exact slot was claimed; "reoffered" means it couldn't be found
#   (already taken, or stale) and a different real slot was claimed instead.
#   Idempotent per call_id: a replay returns the same reservation (via a
#   Redis marker keyed by call_id, TTL'd at AIRCARE_SLOT_TTL_SECONDS) rather
#   than claiming a second slot for the same call. "no_slot" is never cached
#   — a later replay is free to retry in case a slot has since opened up.
# 422 FastAPI's standard validation-error body on missing/invalid required
#   fields, on either endpoint (bodies/query params are typed Pydantic models
#   from schema.py — see VerifyIdentityRequest, NextSlotRequest,
#   ReserveSlotRequest).
# 502 {"error": "mongo_mcp_failure" | "redis_mcp_failure"} on a genuine MCP
#   transport failure — never silently swallowed as a business-logic outcome.
# ---------------------------------------------------------------------------

SLOTS_QUEUE_KEY = "slots:available"
SLOT_RESERVATION_TTL_SECONDS = int(os.getenv("AIRCARE_SLOT_TTL_SECONDS", "900"))
SLOT_LIST_MAX_COUNT = 10  # NextSlotRequest.count's upper bound, and reserve_slot's claim search window below — must match, so reserve_slot can always find whatever /next_slot showed
_DEMO_SLOT_DOCTORS = ["Dr. Rao", "Dr. Iyer", "Dr. Khan", "Dr. Mehta"]
_DEMO_SLOT_COUNT = 12
_DEMO_SLOT_SPACING_MINUTES = 30

_slots_seed_lock = threading.Lock()
_slots_seeded = False


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


def _ensure_slots_seeded():
    """Demo/seed scaffolding only: a real deployment would have slots:available
    populated by an actual doctor-scheduling system, not generated here.
    Guarded so a transient Redis failure retries on the next call instead of
    permanently skipping seeding for the life of the process."""
    global _slots_seeded
    if _slots_seeded:
        return
    with _slots_seed_lock:
        if _slots_seeded:
            return
        if redis_llen(SLOTS_QUEUE_KEY) == 0:
            base = (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=4, minute=0, second=0, microsecond=0)
            for i in range(_DEMO_SLOT_COUNT):
                slot_time = base + timedelta(minutes=_DEMO_SLOT_SPACING_MINUTES * i)
                slot = {
                    "slot_id": f"SLOT-{slot_time.strftime('%Y%m%dT%H%MZ')}",
                    "doctor_name": _DEMO_SLOT_DOCTORS[i % len(_DEMO_SLOT_DOCTORS)],
                    "date_time": slot_time.isoformat(),
                }
                redis_rpush(SLOTS_QUEUE_KEY, _b64_json_encode(slot))
        _slots_seeded = True


def _normalize_dob(value):
    """Parse a spoken or stored date-of-birth into a plain date for
    comparison; None (never a guess) if it can't be parsed at all."""
    if not value:
        return None
    try:
        return dateutil_parser.parse(str(value), fuzzy=True).date()
    except (ValueError, TypeError, OverflowError):
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

    stored_dob = _normalize_dob(patient.get("dob"))
    spoken_dob_parsed = _normalize_dob(spoken_dob)
    if stored_dob is None:
        logger.warning(f"verify_identity: patient_id={patient_id} has no usable dob on record")
        return {"verified": False, "reason": "Looking up patient record failed — please try again or contact support"}
    if spoken_dob_parsed is None or spoken_dob_parsed != stored_dob:
        return {"verified": False, "reason": "DOB mismatch — please try again or contact support"}
    return {"verified": True, "reason": "Continue to teleconsultation slot booking"}


@router.get("/next_slot", response_model=NextSlotResponse, responses={502: {"model": BookingErrorResponse}})
def next_slot(params: Annotated[NextSlotRequest, Query()]):
    call_id, count = params.call_id, params.count
    logger.warning(f"next_slot request: call_id={call_id} count={count}")
    try:
        _ensure_slots_seeded()
        peeked = redis_lrange(SLOTS_QUEUE_KEY, 0, count - 1)
    except RedisMCPError as e:
        logger.exception(f"next_slot: Redis MCP failure call_id={call_id}")
        return _im_error(502, "redis_mcp_failure", str(e), call_id=call_id)

    slots = [_b64_json_decode(raw) for raw in peeked]
    return {"available": bool(slots), "slots": slots}


def _claim_slot(candidate_slot_id):
    """Best-effort claim of a *specific* slot_id out of slots:available, for
    when the caller picked something other than the very first slot
    /next_slot showed. Built from lpop/rpush since the Redis MCP toolset has
    no LREM (see redis_mcp_client.py's module docstring for the exact
    primitives exposed): pops one record at a time, up to SLOT_LIST_MAX_COUNT
    of them — the same window /next_slot can ever show — immediately
    rpushing back anything that doesn't match. Not atomic across concurrent
    reserve_slot calls, and a miss re-queues the skipped slots behind
    whatever it never touched rather than preserving their original
    position — acceptable for this demo/seed scaffold, where no slot is ever
    lost or double-booked, only reordered. Returns the claimed slot dict, or
    None if candidate_slot_id wasn't found in the window (already taken, or
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
    call_id, patient_id, candidate_slot_id = body.call_id, body.patient_id, body.candidate_slot_id
    logger.warning(f"reserve_slot request: call_id={call_id} patient_id={patient_id} candidate_slot_id={candidate_slot_id}")

    reservation_key = f"slot_reservation:{call_id}"
    try:
        existing_raw = _get_existing_reservation(reservation_key)
        if existing_raw is not None:
            return existing_raw

        _ensure_slots_seeded()
        claimed = _claim_slot(candidate_slot_id)
        reoffered = None
        if claimed is None:
            reoffered_raw = redis_lpop(SLOTS_QUEUE_KEY)
            reoffered = _b64_json_decode(reoffered_raw) if reoffered_raw else None
    except RedisMCPError as e:
        logger.exception(f"reserve_slot: Redis MCP failure call_id={call_id}")
        return _im_error(502, "redis_mcp_failure", str(e), call_id=call_id)

    if claimed is not None:
        result = {"result": "reserved", "slot_id": claimed["slot_id"], "date_time": claimed["date_time"], "doctor_name": claimed["doctor_name"]}
    elif reoffered is not None:
        result = {"result": "reoffered", "slot_id": reoffered["slot_id"], "date_time": reoffered["date_time"], "doctor_name": reoffered["doctor_name"]}
    else:
        result = {"result": "no_slot", "slot_id": None, "date_time": None, "doctor_name": None}
    _persist_booking_event(call_id, patient_id, result)
    if result["result"] != "no_slot":  # never cache no_slot — a later replay should retry
        try:
            redis_set(reservation_key, _b64_json_encode(result), expiration=SLOT_RESERVATION_TTL_SECONDS)
        except RedisMCPError:
            logger.exception(f"reserve_slot: failed to persist reservation idempotency marker call_id={call_id}")
    return result


def _get_existing_reservation(reservation_key):
    raw = redis_get(reservation_key)
    if not raw:
        return None
    try:
        return _b64_json_decode(raw)
    except (ValueError, TypeError):
        return None  # corrupt marker — treat as no prior reservation rather than erroring


def _persist_booking_event(call_id, patient_id, result):
    try:
        mcp_insert(
            "call_events",
            {
                "call_id": call_id, "patient_id": patient_id, "event": "teleconsultation_booking",
                "result": result.get("result"), "slot_id": result.get("slot_id"),
                "date_time": result.get("date_time"), "doctor_name": result.get("doctor_name"),
                "created_at": _iso_now(),
            },
            call_id=call_id, dedupe_filter={"call_id": call_id, "event": "teleconsultation_booking"},
        )
    except MongoMCPError:
        logger.exception(f"reserve_slot: failed to persist booking audit doc call_id={call_id}")
