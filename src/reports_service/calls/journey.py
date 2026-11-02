"""A customer's call journey for the call-detail "Dev logs" view: every call
placed to the same phone number as a given call, in time order, each with the
retry-scheduler state it recorded (`sequence_number`, `trigger_call`,
`next_trigger_date`, `call_sequence_mapping`, ...) and a list of detected
inconsistencies.

The phone number is only ever used server-side to find the sibling calls. It
is never returned (only a masked last-4 hint), and every other personal-data
field stays out of the entries — see schema_adapters' module docstring.

Inconsistency checks are deliberately conservative heuristics over the
scheduler's own recorded state; each issue's message says what was observed
and, where it is an inference, says "possible"/"likely".
"""

import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from beanie import PydanticObjectId
from motor.motor_asyncio import AsyncIOMotorCollection

from reports_service.calls.mapped_fields import _deep_json_safe
from reports_service.calls.model import Call

# A call may start slightly before the previous call's `next_trigger_date`
# (scheduler tick granularity); only flag clearly early triggers.
EARLY_TRIGGER_TOLERANCE_SECONDS = 60

_PHONE_FIELDS = ("phone_number", "customerPhone", "phone")
_WRAPPED_ISODATE = re.compile(r"^ISODate\((.+)\)$")


def phone_last10(doc: dict | None) -> str | None:
    """The last 10 digits of the first phone field present, or None."""

    for field in _PHONE_FIELDS:
        value = (doc or {}).get(field)
        if value in (None, ""):
            continue
        if isinstance(value, float):
            value = int(value)
        digits = re.sub(r"\D", "", str(value))
        if len(digits) >= 10:
            return digits[-10:]
    return None


def phone_query(last10: str) -> dict:
    """Matches the number however a source stores it (int, bare string, or
    country-code-prefixed string)."""

    return {
        "$or": [
            {"phone_number": {"$in": [int(last10), last10]}},
            {"customerPhone": last10},
            {"phone": {"$regex": f"{last10}$"}},
        ]
    }


def _parse_dt(value: Any) -> datetime | None:
    """Tolerant timestamp parse -> naive UTC (the scheduler fields are naive
    strings in the same clock as the call times)."""

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        match = _WRAPPED_ISODATE.match(text)
        if match:
            text = match.group(1)
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _to_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.strip().lower() in ("true", "1", "yes"):
            return True
        if value.strip().lower() in ("false", "0", "no"):
            return False
    return None


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _latest_mapping_day(mapping: Any) -> str | None:
    return max(mapping) if isinstance(mapping, dict) and mapping else None


def _humanize(seconds: float) -> str:
    seconds = int(abs(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


def _issue(code: str, severity: str, message: str) -> dict:
    return {"code": code, "severity": severity, "message": message}


def build_entry(call: Call, doc: dict) -> dict:
    mapping = doc.get("call_sequence_mapping")
    day = _latest_mapping_day(mapping)
    today = mapping.get(day) if day and isinstance(mapping.get(day), dict) else {}
    # The source's own callStartTime is authoritative; the adapter's
    # call_start_time can fall back to the (date-only) tempDate when a source
    # format isn't recognised, which would show midnight UTC (05:30 IST).
    start = _parse_dt(doc.get("callStartTime")) or _parse_dt(call.call_start_time)
    return {
        "conversation_id": call.conversation_id,
        "call_start_time": _iso(start),
        "call_end_time": _iso(_parse_dt(call.call_end_time)),
        "call_status": call.call_status,
        "disposition": call.disposition,
        "disposition_raw": _deep_json_safe(doc.get("DISPOSITION")),
        "stage_code": doc.get("STAGE_CODE") or doc.get("stage"),
        "dial_status": doc.get("gnanivb_dial_status"),
        "hangup_reason": doc.get("gnanivb_hangup_cause_text_tech") or doc.get("gnanivb_hangup_cause_text_ast"),
        "ringing_seconds": doc.get("ringingTime"),
        "connected_seconds": call.connected_seconds,
        "sequence_number": _to_int(doc.get("sequence_number")),
        "answered_seq": _to_int(doc.get("answered_seq")),
        "no_answer_seq": _to_int(doc.get("no_answer_seq")),
        "trigger_call": _to_bool(doc.get("trigger_call")),
        "next_trigger_date": _iso(_parse_dt(doc.get("next_trigger_date"))),
        "last_triggered_date": _iso(_parse_dt(doc.get("last_triggered_date"))),
        "post_call_extraction_v2": _deep_json_safe(doc.get("post_call_extraction_v2")),
        "call_sequence_mapping": _deep_json_safe(mapping),
        "day": day,
        "attempt_today": _to_int(today.get("calls_triggered")),
        "max_trigger_today": _to_int(today.get("max_trigger")),
        # Filled by annotate().
        "scheduled_for": None,
        "seconds_vs_schedule": None,
        "issues": [],
    }


def annotate(entries: list[dict]) -> None:
    """Mutates time-ordered `entries` in place: adds `scheduled_for` /
    `seconds_vs_schedule` (this call's start vs the previous call's
    `next_trigger_date`) and an `issues` list per entry."""

    by_seq: dict[int, list[dict]] = {}
    for e in entries:
        if e["sequence_number"] is not None:
            by_seq.setdefault(e["sequence_number"], []).append(e)

    observed_per_day: dict[str, int] = {}
    prev: dict | None = None
    for e in entries:
        issues = e["issues"]
        seq = e["sequence_number"]

        if seq is not None and len(by_seq[seq]) > 1:
            others = [o["conversation_id"] for o in by_seq[seq] if o is not e]
            issues.append(
                _issue(
                    "duplicate_sequence_number",
                    "error",
                    f"sequence_number {seq} is also on {len(others)} other call(s) ({', '.join(others)}). "
                    "Two triggers were fired from the same contact state — possible causes: the scheduler "
                    "picked the contact up twice (overlapping runs / retry race), or the contact record "
                    "was not updated before the next trigger.",
                )
            )

        if prev is not None:
            prev_seq = prev["sequence_number"]
            if seq is not None and prev_seq is not None and seq != prev_seq and seq != prev_seq + 1:
                direction = "jumped ahead" if seq > prev_seq else "went backwards"
                issues.append(
                    _issue(
                        "sequence_gap",
                        "warning",
                        f"sequence_number {direction} from {prev_seq} to {seq}"
                        + (" — a call record may be missing." if seq > prev_seq else "."),
                    )
                )

            scheduled = _parse_dt(prev["next_trigger_date"])
            start = _parse_dt(e["call_start_time"])
            if scheduled and start:
                delta = (start - scheduled).total_seconds()
                e["scheduled_for"] = _iso(scheduled)
                e["seconds_vs_schedule"] = delta
                if delta < -EARLY_TRIGGER_TOLERANCE_SECONDS:
                    issues.append(
                        _issue(
                            "early_trigger",
                            "warning",
                            f"Started {_humanize(delta)} before the previous call's next_trigger_date "
                            f"({prev['next_trigger_date']}) — the retry schedule was not respected.",
                        )
                    )

            if prev["trigger_call"] is False:
                issues.append(
                    _issue(
                        "called_after_stop",
                        "error",
                        "The previous call recorded trigger_call=false (do not call again), but this call was placed.",
                    )
                )

        if e["day"]:
            observed_per_day[e["day"]] = observed_per_day.get(e["day"], 0) + 1
            recorded = e["attempt_today"]
            observed = observed_per_day[e["day"]]
            if recorded is not None and recorded != observed:
                issues.append(
                    _issue(
                        "mapping_count_mismatch",
                        "warning",
                        f"call_sequence_mapping[{e['day']}] says {recorded} call(s) triggered that day, "
                        f"but this is call #{observed} of the day in the logs.",
                    )
                )
            if recorded is not None and e["max_trigger_today"] is not None and recorded > e["max_trigger_today"]:
                issues.append(
                    _issue(
                        "over_max_trigger",
                        "error",
                        f"calls_triggered ({recorded}) exceeds max_trigger ({e['max_trigger_today']}) for {e['day']}.",
                    )
                )
        prev = e


def summarize(entries: list[dict]) -> dict:
    day_summary: dict[str, dict] = {}
    for e in entries:  # later calls carry the fuller mapping
        if isinstance(e["call_sequence_mapping"], dict):
            day_summary.update(e["call_sequence_mapping"])
    last = entries[-1] if entries else None
    return {
        "total_calls": len(entries),
        "answered_calls": sum(1 for e in entries if str(e["call_status"]).upper() == "ANSWERED"),
        "calls_with_issues": sum(1 for e in entries if e["issues"]),
        "first_call_time": entries[0]["call_start_time"] if entries else None,
        "last_call_time": last["call_start_time"] if last else None,
        "next_trigger_date": last["next_trigger_date"] if last else None,
        "next_trigger_call": last["trigger_call"] if last else None,
        "day_summary": day_summary,
    }


def mask_phone(last10: str) -> str:
    return "******" + last10[-4:]


async def load_journey(
    collection: AsyncIOMotorCollection,
    source_doc: dict,
    *,
    adapter: Callable[..., dict] | None,
    project_id: str,
    organization_id: str,
) -> dict:
    """Builds the journey for the customer behind `source_doc`.

    Non-adapter (native Call-schema) sources are additionally scoped to
    `project_id`, matching get_by_conversation_id; adapter sources carry no
    project_id (see schema_adapters), matching every other adapter read.
    """

    last10 = phone_last10(source_doc)
    if last10 is None:
        return {
            "available": False,
            "reason": "This call's source record has no phone number, so other calls can't be linked to it.",
            "phone_masked": None,
            "summary": summarize([]),
            "entries": [],
        }

    query = phone_query(last10)
    if adapter is None:
        query = {"$and": [query, {"project_id": PydanticObjectId(project_id)}]}

    entries: list[dict] = []
    async for doc in collection.find(query):
        if adapter is not None:
            call = Call.model_validate(adapter(doc, project_id=project_id, organization_id=organization_id))
        else:
            call = Call.model_validate(doc)
        entries.append(build_entry(call, doc))

    entries.sort(key=lambda e: (e["call_start_time"] or "", e["sequence_number"] or 0))
    annotate(entries)
    return {
        "available": True,
        "reason": None,
        "phone_masked": mask_phone(last10),
        "summary": summarize(entries),
        "entries": entries,
    }
