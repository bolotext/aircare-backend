"""Validation of customer-details payloads against a project's configured
field schema (`[{"key": ..., "type": <Mongo $type alias>}]`, see the users
service's Project.customer_details_fields). Only keys in the schema are
accepted, and each value is checked and converted to the declared BSON type
before it is written.

Types: string, int (int32), long (int64), double, decimal (Decimal128, sent as
a numeric string), bool, date (ISO-8601 string), objectId (24-hex string),
array, object (JSON).
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from bson import Decimal128, Int64, ObjectId
from common.errors import UnprocessableEntityError

MAX_STRING_LENGTH = 1000
INT32_MIN, INT32_MAX = -(2**31), 2**31 - 1
INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1

_LEGACY_TYPES = {"text": "string", "number": "double", "boolean": "bool"}


def normalize_type(type_: str) -> str:
    return _LEGACY_TYPES.get(type_, type_)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _coerce(type_: str, value: Any) -> Any:
    if value is None:
        return None
    if type_ == "string":
        if not isinstance(value, str) or len(value) > MAX_STRING_LENGTH:
            raise ValueError(f"must be a string up to {MAX_STRING_LENGTH} characters")
        return value
    if type_ == "int":
        if not _is_int(value) or not INT32_MIN <= value <= INT32_MAX:
            raise ValueError("must be a 32-bit integer")
        return value
    if type_ == "long":
        if not _is_int(value) or not INT64_MIN <= value <= INT64_MAX:
            raise ValueError("must be a 64-bit integer")
        return Int64(value)
    if type_ == "double":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("must be a number")
        return float(value)
    if type_ == "decimal":
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError("must be a decimal number")
        try:
            return Decimal128(Decimal(str(value)))
        except (InvalidOperation, ValueError):
            raise ValueError("must be a decimal number")
    if type_ == "bool":
        if not isinstance(value, bool):
            raise ValueError("must be true or false")
        return value
    if type_ == "date":
        if not isinstance(value, str):
            raise ValueError("must be an ISO date string")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    if type_ == "objectId":
        if not isinstance(value, str) or not ObjectId.is_valid(value):
            raise ValueError("must be a 24-character hex ObjectId")
        return ObjectId(value)
    if type_ == "array":
        if not isinstance(value, list):
            raise ValueError("must be an array")
        return value
    if type_ == "object":
        if not isinstance(value, dict):
            raise ValueError("must be an object")
        return value
    raise ValueError("unknown field type")


def _option_value(type_: str, option: str) -> Any:
    """Options are stored as strings; convert one to the field's type for comparison."""

    try:
        if type_ in ("int", "long"):
            return _coerce(type_, int(option))
        if type_ == "double":
            return _coerce(type_, float(option))
        return _coerce(type_, option)
    except ValueError:
        raise ValueError(f"option {option!r} is not a valid {type_}")


def _comparable(value: Any) -> Any:
    return value.to_decimal() if isinstance(value, Decimal128) else value


def validate_fields(fields: dict[str, Any], schema: list[dict]) -> dict[str, Any]:
    types = {f["key"]: normalize_type(f["type"]) for f in schema}
    options = {f["key"]: f.get("options") or [] for f in schema}
    errors: dict[str, str] = {}
    clean: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in types:
            errors[key] = "not a configured customer field"
            continue
        try:
            clean[key] = _coerce(types[key], value)
            if options[key] and clean[key] is not None:
                allowed = [_comparable(_option_value(types[key], opt)) for opt in options[key]]
                if _comparable(clean[key]) not in allowed:
                    raise ValueError("must be one of: " + ", ".join(options[key]))
        except ValueError as exc:
            errors[key] = str(exc)
    if errors:
        raise UnprocessableEntityError("Invalid customer details", details=errors)
    return clean


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, Decimal128):
        return str(value.to_decimal())
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def serialize(doc: dict, schema: list[dict], include_other: bool = False) -> dict:
    """List/create/update return only configured keys. The single-record
    detail view (`include_other=True`) also returns every other key present
    in the document under `other_fields`, so the full record can be seen."""

    out = {
        "id": str(doc["_id"]),
        "fields": {spec["key"]: _json_safe(doc.get(spec["key"])) for spec in schema},
    }
    if include_other:
        configured = {spec["key"] for spec in schema}
        out["other_fields"] = {str(k): _json_safe(v) for k, v in doc.items() if k != "_id" and k not in configured}
    return out
