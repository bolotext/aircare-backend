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

# Product decision (2026-11-02): no source field is withheld any more — the call
# list columns and the detail/raw-JSON views return every value to users who
# already hold `report.view` for the project. Re-add field names here to
# restore masking for them (keys lower-cased).
_PII_RAW_FIELDS: set[str] = set()


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


_IMMUTABLE_KEYS = {"_id"}
_AUDIT_KEYS = {"source_edited_by", "source_edited_at"}


def diff_source_document(current: dict | None, edited: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Compares the edited detail-view document against the stored one and
    returns `(to_set, to_unset)`. Only genuinely changed keys are returned, so
    values the detail view serialised lossily (ObjectId/datetime -> string) keep
    their stored BSON type when untouched. Raises ValueError when the edit
    touches `_id`, a withheld personal-data field, or uses an unsafe key."""

    current = current or {}
    shown = source_document(current)
    to_set: dict[str, Any] = {}
    for key, value in edited.items():
        if not isinstance(key, str) or not key or key.startswith("$") or "." in key:
            raise ValueError(f"Invalid field name: {key!r}")
        if key in shown and shown[key] == value:
            continue
        if key in _IMMUTABLE_KEYS or key in _AUDIT_KEYS:
            raise ValueError(f"Field '{key}' cannot be edited")
        if is_restricted(key) and key.lower() not in _DETAIL_ALLOWED:
            raise ValueError(f"Field '{key}' holds personal data and cannot be edited")
        to_set[key] = value
    to_unset = [k for k in current if k not in edited]
    for key in to_unset:
        if key in _IMMUTABLE_KEYS or key in _AUDIT_KEYS or (is_restricted(key) and key.lower() not in _DETAIL_ALLOWED):
            raise ValueError(f"Field '{key}' cannot be removed")
    return to_set, to_unset
