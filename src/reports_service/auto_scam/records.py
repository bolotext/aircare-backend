import re

from bson import ObjectId

from reports_service.auto_scam.model import RECORDS_COLLECTION
from reports_service.core.config import get_settings
from reports_service.db.mongodb import get_client

KEY_FIELDS = ("customerCRTId", "conversation_id", "sender_id")
KEY_PROJECTION = {"_id": 1, **{k: 1 for k in KEY_FIELDS}}


def records_collection():
    """Raw `auto_scam` collection (conversation shape is free-form, so no Beanie model)."""
    return get_client()[get_settings().mongo_db_name][RECORDS_COLLECTION]


def record_filter(aircare_project_id, file_name: str, q: str = "") -> dict:
    flt: dict = {"aircare_project_id": aircare_project_id, "project_id": file_name}
    if q:
        rx = {"$regex": re.escape(q), "$options": "i"}
        flt["$or"] = [{"customerCRTId": rx}, {"STAGE_CODE": rx}, {"DISPOSITION": rx}, {"INTEREST_CLASSIFICATION": rx}]
    return flt


async def ensure_indexes() -> None:
    """Indexes on the raw `auto_scam` collection (it has no Beanie model, so they are created here at startup).
    customerCRTId is not unique: a raw upload may legitimately contain duplicates."""
    col = records_collection()
    await col.create_index([("customerCRTId", 1)], name="customerCRTId_1")
    await col.create_index([("aircare_project_id", 1), ("project_id", 1), ("customerCRTId", 1)], name="project_file_customerCRTId")


def record_key(doc: dict) -> str:
    """Identity of a conversation across runs: its customerCRTId (falling back to conversation_id / sender_id,
    then the Mongo id when the export has none). Analytics results are stored and looked up by this key."""
    for field in KEY_FIELDS:
        value = doc.get(field)
        if value is not None and str(value).strip():
            return str(value).strip()
    return f"rid:{doc['_id']}"
