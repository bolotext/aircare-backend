import re
from datetime import date, datetime, time, timedelta, timezone

from bson import ObjectId

from reports_service.auto_scam.model import RECORDS_COLLECTION
from reports_service.core.config import get_settings
from reports_service.db.mongodb import get_client

KEY_FIELDS = ("customerCRTId", "conversation_id", "sender_id")
KEY_PROJECTION = {"_id": 1, **{k: 1 for k in KEY_FIELDS}}


def records_collection():
    """Raw `auto_scam` collection (conversation shape is free-form, so no Beanie model)."""
    return get_client()[get_settings().mongo_db_name][RECORDS_COLLECTION]


EMPTY_VALUE = "__empty__"  # filter choice meaning "the field is null / missing / blank"
MAX_DATE_SPAN_DAYS = 8000
FILTER_FIELDS = (("dispositions", "DISPOSITION"), ("stage_codes", "STAGE_CODE"), ("classifications", "INTEREST_CLASSIFICATION"))


def record_filter(aircare_project_id, file_name: str, filters: dict | None = None) -> dict:
    """Mongo filter for one file's conversations. `filters` (all optional): q (text), dispositions / stage_codes /
    classifications (lists, any-of), date_from / date_to (YYYY-MM-DD, inclusive, on `tempDate` only)."""
    f = filters or {}
    flt: dict = {"aircare_project_id": aircare_project_id, "project_id": file_name}
    if f.get("q"):
        rx = {"$regex": re.escape(f["q"]), "$options": "i"}
        flt["$or"] = [{"customerCRTId": rx}, {"STAGE_CODE": rx}, {"DISPOSITION": rx}, {"INTEREST_CLASSIFICATION": rx}]
    for key, field in FILTER_FIELDS:
        if f.get(key):
            values = [v for v in f[key] if v != EMPTY_VALUE]
            if len(values) == len(f[key]):
                flt[field] = {"$in": values}
            else:  # "(empty / null)" ticked: also match null, missing and blank values
                blank = [{field: None}, {field: {"$regex": r"^\s*$"}}]
                flt.setdefault("$and", []).append({"$or": blank + ([{field: {"$in": values}}] if values else [])})
    if f.get("date_from") or f.get("date_to"):
        flt.setdefault("$and", []).append(_date_clause(f.get("date_from"), f.get("date_to")))
    return flt


def _date_clause(date_from: str | None, date_to: str | None) -> dict:
    """Date filter on `tempDate` only (DD/MM/YYYY text, so it is matched as a list of days, not a string range).
    The conversation itself (message timestamps / call_date) is not consulted: a record with no tempDate never matches."""
    today = datetime.combine(date.today(), time.min)
    start = datetime.strptime(date_from, "%Y-%m-%d") if date_from else None
    end = datetime.strptime(date_to, "%Y-%m-%d") if date_to else None
    if start is None:
        start = (end or today) - timedelta(days=3650)
    if end is None:
        end = max(start, today + timedelta(days=366))
    if end < start:
        return {"tempDate": {"$in": []}}
    start = max(start, end - timedelta(days=MAX_DATE_SPAN_DAYS - 1))
    texts = [(start + timedelta(days=i)).strftime("%d/%m/%Y") for i in range((end - start).days + 1)]
    return {"tempDate": {"$in": texts}}


async def with_analytics(flt: dict, aircare_project_id, file_name: str, mode: str | None) -> dict:
    """Narrow `flt` to conversations whose analytics are done ("done": a successful result exists for their key) or
    not done ("pending"). Results are keyed by customerCRTId (falling back to conversation_id / sender_id), so the key
    list is matched against those fields. Any other `mode` leaves `flt` as it is."""
    if mode not in ("done", "pending"):
        return flt
    from reports_service.auto_scam.model import AutoScamResult  # local: model imports nothing from here, keep it cheap

    cursor = AutoScamResult.get_pymongo_collection().find(
        {"aircare_project_id": aircare_project_id, "file_name": file_name, "error": None, "fields": {"$ne": []}},
        {"customer_crt_id": 1},
    )
    keys = [d["customer_crt_id"] async for d in cursor]
    match = [{f: {"$in": keys}} for f in KEY_FIELDS]
    clause = {"$or": match} if mode == "done" else {"$nor": match}
    return {"$and": [flt, clause]}


_DATE_FORMATS = ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%y", "%d-%m-%y")


def _parse_date_text(text: str) -> str | None:
    """YYYY-MM-DD from a date string, tolerating a trailing time ("26/10/2026 14:03", "2026-10-26T14:03:00Z")."""
    text = text.strip()
    if not text:
        return None
    head = text.replace("T", " ").split(" ")[0]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(head, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _parse_timestamp(ts) -> str | None:
    if isinstance(ts, bool):
        return None
    if isinstance(ts, str):
        ts = ts.strip()
        try:
            ts = float(ts)
        except ValueError:
            return _parse_date_text(ts)
    if not isinstance(ts, (int, float)):
        return None
    if ts > 1e17:    # nanoseconds
        ts /= 1e9
    elif ts > 1e14:  # microseconds
        ts /= 1e6
    elif ts > 1e11:  # milliseconds
        ts /= 1e3
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return None


def derive_call_date(rec: dict) -> str | None:
    """The call's date as YYYY-MM-DD: `tempDate` if it parses (DD/MM/YYYY first, other common formats too), else the
    first message with a usable timestamp (seconds / ms / us / ISO text; UTC)."""
    day = _parse_date_text(str(rec.get("tempDate") or ""))
    if day:
        return day
    for msg in rec.get("conversation_log") or []:
        if isinstance(msg, dict) and msg.get("timestamp") is not None:
            day = _parse_timestamp(msg["timestamp"])
            if day:
                return day
    return None


async def ensure_indexes() -> None:
    """Indexes on the raw `auto_scam` collection (it has no Beanie model, so they are created here at startup).
    customerCRTId is not unique: a raw upload may legitimately contain duplicates."""
    col = records_collection()
    await col.create_index([("customerCRTId", 1)], name="customerCRTId_1")
    await col.create_index([("aircare_project_id", 1), ("project_id", 1), ("customerCRTId", 1)], name="project_file_customerCRTId")
    await col.create_index([("aircare_project_id", 1), ("project_id", 1), ("call_date", 1)], name="project_file_call_date")
    # conversations uploaded before call_date existed (or when it could not be derived): (re)derive it at startup
    async for d in col.find({"$or": [{"call_date": {"$exists": False}}, {"call_date": None}]}, {"tempDate": 1, "conversation_log": 1, "call_date": 1}):
        day = derive_call_date(d)
        if day is not None or "call_date" not in d:  # still undated documents already hold null: leave them
            await col.update_one({"_id": d["_id"]}, {"$set": {"call_date": day}})


def record_key(doc: dict) -> str:
    """Identity of a conversation across runs: its customerCRTId (falling back to conversation_id / sender_id,
    then the Mongo id when the export has none). Analytics results are stored and looked up by this key."""
    for field in KEY_FIELDS:
        value = doc.get(field)
        if value is not None and str(value).strip():
            return str(value).strip()
    return f"rid:{doc['_id']}"
