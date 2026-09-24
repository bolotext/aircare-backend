"""Per-source-collection document normalizers.

`Call` (see calls/model.py) is this service's own canonical call shape. A
project's configured report db/collection (see
core.deps.resolve_project_source) is not guaranteed to already be in it —
e.g. `demo_collection.report_llm` is a raw call-center CDR export with its
own field names (`callStartTime`, `gnanivb_dial_status`, ...) and no
`project_id`/`conversation_id`/`call_start_time` at all.

An adapter here turns one raw source document into a plain dict shaped like
`Call`'s fields, filled in with the project_id/organization_id of whichever
project is being queried (the source schema itself carries neither). The
result is fed through `Call.model_validate()` exactly like a native
document (see calls.repository.fetch_normalized_calls) — never persisted, `id`
stays None — so the rest of the pipeline (filtering, pagination,
aggregation, and the existing CallListItem/CallDetail rendering) keeps
working against one field set regardless of where the data actually came
from.

PII the source schema carries — customer name, phone numbers
(`phone`/`phone_number`/`customerPhone`/`srcPhone`/`dstPhone`), the call
recording path (`rec_path`), and the raw conversation transcript
(`conversation_log`) — is deliberately dropped here, never copied into
`raw`: this service's API must not return it (flagged to the project owner
as real-shaped customer/PII data sitting in a project literally named
"demo").

Which normalizer (if any) applies to a given project is *detected*, not
configured: `resolve_adapter` below samples one document from the project's
configured collection and checks it against each normalizer's `_is_*_shape`
predicate. Connecting a new project whose collection is already in a
recognized shape — another `report_llm`-style or `aircare.calls`-style CDR
export, whatever database/collection name it lives under — needs no code
change here at all. Only a genuinely new raw shape needs a new
normalize/`_is_*_shape` pair added to `_DETECTORS`.
"""

import re
from collections.abc import Callable
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorCollection


def _parse_gnani_timestamp(value: str | None) -> datetime | None:
    """Source format: "2025/02/13 14:56:32 +0000"."""

    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y/%m/%d %H:%M:%S %z")
    except ValueError:
        return None


def _to_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def normalize_report_llm(doc: dict, *, project_id: str, organization_id: str) -> dict:
    call_start = _parse_gnani_timestamp(doc.get("callStartTime"))
    if call_start is None and doc.get("tempDate"):
        try:
            call_start = datetime.strptime(doc["tempDate"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            call_start = None

    return {
        "project_id": project_id,
        "organization_id": organization_id,
        "conversation_id": str(doc.get("call_uid") or doc.get("customerCRTId") or doc["_id"]),
        "bot_id": doc.get("flow_id") or "unknown",
        "language": "unknown",
        "call_status": doc.get("callStatus") or doc.get("call_status") or "UNKNOWN",
        "disposition": doc.get("gnanivb_dial_status") or doc.get("stage") or "UNKNOWN",
        "call_start_time": call_start or datetime.now(timezone.utc),
        "call_connected_time": _parse_gnani_timestamp(doc.get("callConnectedTime")),
        "call_end_time": _parse_gnani_timestamp(doc.get("callEndTime")),
        "connected_seconds": _to_float(doc.get("gnanivb_billing_duration")),
        "tool_failure_count": 0,
        # No name/phone/phone_number/customerPhone/srcPhone/dstPhone/
        # rec_path/conversation_log here — see module docstring.
        "raw": {
            "call_sequence_mapping": doc.get("call_sequence_mapping"),
            "sequence_number": doc.get("sequence_number"),
            "answered_seq": doc.get("answered_seq"),
            "no_answer_seq": doc.get("no_answer_seq"),
            "stage": doc.get("stage"),
            "ringing_time_seconds": doc.get("ringingTime"),
            "setup_time_seconds": doc.get("setupTime"),
            "hangup_cause_code": doc.get("gnanivb_hangup_cause_code"),
            "hangup_cause_text": doc.get("gnanivb_hangup_cause_text_tech"),
            "dial_status": doc.get("gnanivb_dial_status"),
        },
    }


def normalize_aircare_calls(doc: dict, *, project_id: str, organization_id: str) -> dict:
    """`aircare.calls` (configured on the "proj-a" demo project) is a fuller
    export from the same raw voice-platform shape as `report_llm` — same
    field names for the pieces both share (`callStartTime`, `flow_id`,
    `gnanivb_dial_status`, ...), but `callStartTime`/`callConnectedTime`/
    `callEndTime` are already native BSON datetimes here (not the
    "YYYY/MM/DD HH:MM:SS +0000" strings `_parse_gnani_timestamp` expects),
    `tempDate` is `DD/MM/YYYY` (report_llm's is `YYYY-MM-DD`), and it
    actually carries a usable `language` field.

    Same PII exclusion as normalize_report_llm above: no name/phone/
    phone_number/customerPhone/srcPhone/dstPhone/rec_path/conversation_log/
    user_id/disposition_result/post_call_extraction in the output, here or
    in `raw`.
    """

    def _aware(value: object) -> datetime | None:
        # PyMongo returns naive datetimes for BSON dates (this driver isn't
        # configured tz_aware=True), representing UTC — attach that tzinfo
        # so these compare against build_mongo_query/day_range_bounds'
        # tz-aware windows instead of raising on naive-vs-aware comparison.
        if not isinstance(value, datetime):
            return None
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    call_start = _aware(doc.get("callStartTime"))
    if call_start is None and doc.get("tempDate"):
        try:
            call_start = datetime.strptime(doc["tempDate"], "%d/%m/%Y").replace(tzinfo=timezone.utc)
        except ValueError:
            call_start = None
    call_connected = _aware(doc.get("callConnectedTime"))
    call_end = _aware(doc.get("callEndTime"))

    return {
        "project_id": project_id,
        "organization_id": organization_id,
        "conversation_id": str(doc.get("conversation_id") or doc.get("call_uid") or doc["_id"]),
        "bot_id": doc.get("bot_id") or doc.get("flow_id") or "unknown",
        "language": doc.get("language") or "unknown",
        "call_status": doc.get("call_status") or doc.get("callStatus") or "UNKNOWN",
        "disposition": doc.get("DISPOSITION") or doc.get("STAGE_CODE") or doc.get("gnanivb_dial_status") or "UNKNOWN",
        "call_start_time": call_start or datetime.now(timezone.utc),
        "call_connected_time": call_connected,
        "call_end_time": call_end,
        "connected_seconds": _to_float(doc.get("gnanivb_billing_duration")),
        "tool_failure_count": 0,
        "raw": {
            "call_sequence_mapping": doc.get("call_sequence_mapping"),
            "sequence_number": doc.get("sequence_number"),
            "answered_seq": doc.get("answered_seq"),
            "no_answer_seq": doc.get("no_answer_seq"),
            "stage": doc.get("STAGE_CODE"),
            "ringing_time_seconds": doc.get("ringingTime"),
            "setup_time_seconds": doc.get("setupTime"),
            "hangup_cause_code": doc.get("gnanivb_hangup_cause_code"),
            "hangup_cause_text": doc.get("gnanivb_hangup_cause_text_tech"),
            "dial_status": doc.get("gnanivb_dial_status"),
        },
    }


_YYYY_MM_DD = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DD_MM_YYYY = re.compile(r"^\d{2}/\d{2}/\d{4}$")


def _is_native_call_shape(doc: dict) -> bool:
    """This service's own `Call` schema (see calls/model.py) uses snake_case
    field names — `call_start_time`, `conversation_id` — that no raw CDR
    export in the wild uses (they're all camelCase: `callStartTime`,
    `callStatus`, ...). Cheap, reliable signal that a collection needs no
    normalization at all.
    """

    return "call_start_time" in doc and "conversation_id" in doc


def _is_report_llm_shape(doc: dict) -> bool:
    """`report_llm`-style raw export (see normalize_report_llm): when
    present, `callStartTime` is the "YYYY/MM/DD HH:MM:SS +0000" string
    `_parse_gnani_timestamp` expects; when it's missing outright, `tempDate`
    is `YYYY-MM-DD`.
    """

    call_start = doc.get("callStartTime")
    if isinstance(call_start, str):
        return _parse_gnani_timestamp(call_start) is not None
    if call_start is None:
        temp_date = doc.get("tempDate")
        return isinstance(temp_date, str) and bool(_YYYY_MM_DD.match(temp_date))
    return False


def _is_aircare_calls_shape(doc: dict) -> bool:
    """`aircare.calls`-style raw export (see normalize_aircare_calls): when
    present, `callStartTime` is already a native BSON datetime; when it's
    missing outright, `tempDate` is `DD/MM/YYYY`.
    """

    call_start = doc.get("callStartTime")
    if isinstance(call_start, datetime):
        return True
    if call_start is None:
        temp_date = doc.get("tempDate")
        return isinstance(temp_date, str) and bool(_DD_MM_YYYY.match(temp_date))
    return False


# Order matters: each project's sample document is checked against these in
# turn, first match wins. Add a new (predicate, normalizer) pair here for a
# genuinely new raw shape — no other file needs to change to onboard a new
# project on an already-recognized shape (see module docstring).
_DETECTORS: list[tuple[Callable[[dict], bool], Callable[..., dict]]] = [
    (_is_report_llm_shape, normalize_report_llm),
    (_is_aircare_calls_shape, normalize_aircare_calls),
]


class UnrecognizedReportSchemaError(Exception):
    """Raised when a project's configured collection has data in it but its
    shape doesn't match `Call`'s own schema or any known raw export —
    surfaced loudly (see core.deps.resolve_project_source) rather than
    silently returning an empty call list, which is how this exact
    situation used to fail before detection replaced a hardcoded registry.
    """

    def __init__(self, db_name: str, collection_name: str):
        self.db_name = db_name
        self.collection_name = collection_name
        super().__init__(
            f"{db_name}.{collection_name} doesn't match this service's Call schema or any known raw "
            "export shape — add a normalize_*/_is_*_shape pair to schema_adapters.py for it."
        )


# Detecting a collection's shape costs one Mongo round trip (a single
# find_one); cached per (db, collection) — the Mongo namespace, not
# project_id, since the seeded demo data already has one report collection
# shared across multiple projects — so every request after the first for a
# given collection skips it.
_detected_cache: dict[tuple[str, str], Callable[..., dict] | None] = {}

# `aircare.calls` (see patient_calls/model.py) deliberately shares its
# physical collection with an unrelated document shape: AQI-triggered patient
# advisory-call records written by an external system, keyed by `call_id`
# (never `conversation_id`) and carrying `live_aqi_reading`. Sampling one of
# those here would misdetect the whole collection as an unrecognized raw
# export and 422 every native-Call project pointed at it (e.g. the demo
# projects) — so detection explicitly excludes that shape when picking a
# sample, exactly as if those documents weren't in this collection at all.
_PATIENT_ADVISORY_CALL_EXCLUSION = {"call_id": {"$exists": False}, "live_aqi_reading": {"$exists": False}}


async def resolve_adapter(collection: AsyncIOMotorCollection) -> Callable[..., dict] | None:
    """Figures out how to read `collection`'s documents as `Call`s by
    sampling one document and checking its shape against every known raw
    export format (see _DETECTORS above). Returns None — the native,
    no-adapter query path — when the collection is empty (nothing to
    detect; harmless either way) or already looks like this service's own
    Call schema.
    """

    key = (collection.database.name, collection.name)
    if key in _detected_cache:
        return _detected_cache[key]

    sample = await collection.find_one(_PATIENT_ADVISORY_CALL_EXCLUSION)
    if sample is None:
        # Nothing to detect yet (a newly connected, not-yet-populated
        # project). Not cached: once real documents land, the next request
        # should actually look at them instead of being stuck on this guess.
        return None

    if _is_native_call_shape(sample):
        adapter = None
    else:
        adapter = next((normalize for matches, normalize in _DETECTORS if matches(sample)), None)
        if adapter is None:
            raise UnrecognizedReportSchemaError(*key)

    _detected_cache[key] = adapter
    return adapter
