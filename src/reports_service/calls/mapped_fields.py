"""Resolves a project's Field Mapper (canonical name -> raw source field) into
the per-row `fields` the Conversation Logs table renders.

Values are read from the call's source Mongo document. Raw fields that carry
personal data (see `schema_adapters`' module docstring: the API must not
return customer names, phone numbers, recording paths, health/location data)
are never returned even when an admin maps them: the column is reported as
`restricted` and its values are `null`. Nested objects/arrays and the
conversation transcript are likewise not returned in a list view.
"""

from datetime import datetime
from typing import Any

from bson import ObjectId

_PII_RAW_FIELDS = {
    "phone",
    "phone_number",
    "phonenumber",
    "customerphone",
    "srcphone",
    "dstphone",
    "mobile",
    "rec_path",
    "recording_url",
    "full_name",
    "name",
    "customer_name",
    "customername",
    "email",
    "aadhaar",
    "pan",
    "dob",
    "condition",
    "city",
    "address",
    "customercrtid",
    "user_context",
    "call_infra",
    "conversation_log",
}


def is_restricted(raw_field: str) -> bool:
    return raw_field.strip().lower() in _PII_RAW_FIELDS


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, ObjectId):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return None  # nested dict/list/bytes: not shown in a list view


WITHHELD = "[withheld: personal data]"
# Shown in the call-detail view only (it already renders the transcript).
_DETAIL_ALLOWED = {"conversation_log"}


def _deep_json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _deep_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_deep_json_safe(v) for v in value]
    if isinstance(value, bytes):
        return "<binary>"
    safe = _json_safe(value)
    return safe if safe is not None or value is None else str(value)


def source_document(doc: dict | None) -> dict[str, Any]:
    """The call's full source Mongo document for the detail view. Every
    top-level key is kept so nothing is silently dropped, but values of
    personal-data fields are replaced with a withheld marker."""

    if not doc:
        return {}
    return {
        str(key): WITHHELD
        if is_restricted(str(key)) and str(key).lower() not in _DETAIL_ALLOWED
        else _deep_json_safe(value)
        for key, value in doc.items()
    }


def mapped_columns(mapping: dict[str, str]) -> list[dict]:
    return [
        {"key": name, "raw_field": raw, "restricted": is_restricted(raw)}
        for name, raw in mapping.items()
        if name and raw
    ]


def extract_fields(source_doc: dict | None, mapping: dict[str, str]) -> dict[str, Any]:
    if not source_doc:
        return {}
    return {
        name: None if is_restricted(raw) else _json_safe(source_doc.get(raw))
        for name, raw in mapping.items()
        if name and raw
    }
