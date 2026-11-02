import re
from datetime import datetime, timezone

from bson import ObjectId

from reports_service.auto_scam.model import RECORDS_COLLECTION
from reports_service.core.config import get_settings
from reports_service.db.mongodb import get_client

KEY_FIELDS = ("customerCRTId", "conversation_id", "sender_id")
KEY_PROJECTION = {"_id": 1, **{k: 1 for k in KEY_FIELDS}}


def records_collection():
    """Raw `auto_scam` collection (conversation shape is free-form, so no Beanie model)."""
    return get_client()[get_settings().mongo_db_name][RECORDS_COLLECTION]


FILTER_FIELDS = (("dispositions", "DISPOSITION"), ("stage_codes", "STAGE_CODE"), ("classifications", "INTEREST_CLASSIFICATION"))


def record_filter(aircare_project_id, file_name: str, filters: dict | None = None) -> dict:
    """Mongo filter for one file's conversations. `filters` (all optional): q (text), dispositions / stage_codes /
    classifications (lists, any-of), date_from / date_to (YYYY-MM-DD, inclusive, on `call_date`)."""
    f = filters or {}
    flt: dict = {"aircare_project_id": aircare_project_id, "project_id": file_name}
    if f.get("q"):
        rx = {"$regex": re.escape(f["q"]), "$options": "i"}
        flt["$or"] = [{"customerCRTId": rx}, {"STAGE_CODE": rx}, {"DISPOSITION": rx}, {"INTEREST_CLASSIFICATION": rx}]
    for key, field in FILTER_FIELDS:
        if f.get(key):
            flt[field] = {"$in": list(f[key])}
    date_range = {op: f[key] for key, op in (("date_from", "$gte"), ("date_to", "$lte")) if f.get(key)}
    if date_range:
        flt["call_date"] = date_range
    return flt


def derive_call_date(rec: dict) -> str | None:
    """The call's date as YYYY-MM-DD: `tempDate` (DD/MM/YYYY) if present, else the first message's timestamp (UTC)."""
    temp = str(rec.get("tempDate") or "").strip()
    try:
        return datetime.strptime(temp, "%d/%m/%Y").strftime("%Y-%m-%d")
    except ValueError:
        pass
    for msg in rec.get("conversation_log") or []:
        ts = msg.get("timestamp") if isinstance(msg, dict) else None
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            try:
                return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            except (OverflowError, OSError, ValueError):
                return None
    return None


async def ensure_indexes() -> None:
    """Indexes on the raw `auto_scam` collection (it has no Beanie model, so they are created here at startup).
    customerCRTId is not unique: a raw upload may legitimately contain duplicates."""
    col = records_collection()
    await col.create_index([("customerCRTId", 1)], name="customerCRTId_1")
    await col.create_index([("aircare_project_id", 1), ("project_id", 1), ("customerCRTId", 1)], name="project_file_customerCRTId")
    await col.create_index([("aircare_project_id", 1), ("project_id", 1), ("call_date", 1)], name="project_file_call_date")
    # conversations uploaded before call_date existed: derive it once (documents without a date get null, so they are not revisited)
    async for d in col.find({"call_date": {"$exists": False}}, {"tempDate": 1, "conversation_log": 1}):
        await col.update_one({"_id": d["_id"]}, {"$set": {"call_date": derive_call_date(d)}})


def record_key(doc: dict) -> str:
    """Identity of a conversation across runs: its customerCRTId (falling back to conversation_id / sender_id,
    then the Mongo id when the export has none). Analytics results are stored and looked up by this key."""
    for field in KEY_FIELDS:
        value = doc.get(field)
        if value is not None and str(value).strip():
            return str(value).strip()
    return f"rid:{doc['_id']}"
