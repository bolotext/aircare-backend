"""Copies a customer's prod data, looked up by phone number, into a project's
own databases: every call record (prod report API) goes to the report
collection and the customer record (prod customer API) goes to the project's
customer-details collection. The Flow ID comes from the project's Data Source
settings.

The prod record is personal data (names, phone numbers, transcript). This
runs only for callers holding `report.import` (Admin only — see
users_service.db.seed) after they explicitly confirm the data may be copied
(`confirm_pii`), is logged with who/what/where, and the saved document is
never echoed back — the response only reports where it was written.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from common.errors import BadGatewayError, UnprocessableEntityError
from motor.motor_asyncio import AsyncIOMotorClient

from reports_service.core.config import get_settings

logger = logging.getLogger(__name__)

# Mongo's own bookkeeping databases must never be a write target.
RESERVED_DB_NAMES = {"admin", "local", "config"}
DB_NAME_PATTERN = r"^[A-Za-z0-9_-]{1,38}$"
REPORT_PATH = "/mongo_test_report"
CUSTOMER_PATH = "/mongo_test_customer"
PHONE_NUMBER_PATTERN = r"^[0-9]{6,32}$"


def validate_db_name(db_name: str) -> None:
    if db_name.lower() in RESERVED_DB_NAMES:
        raise UnprocessableEntityError(f"'{db_name}' is a reserved Mongo database and can't be written to")
    if not re.match(DB_NAME_PATTERN, db_name):
        raise UnprocessableEntityError("Database name may only contain letters, digits, '_' and '-'")


async def _fetch(path: str, phone_number: str, flow_id: str, what: str) -> Any:
    settings = get_settings()
    if not settings.prod_import_base_url:
        raise BadGatewayError(f"Prod {what} import is not configured")
    url = settings.prod_import_base_url.rstrip("/") + path
    try:
        async with httpx.AsyncClient(timeout=settings.prod_import_timeout_seconds) as client:
            response = await client.get(
                url,
                params={"number": phone_number},
                headers={"accept": "*/*", "flow_id": flow_id},
            )
    except httpx.HTTPError as exc:
        raise BadGatewayError(f"Unable to reach the prod {what} API") from exc

    if response.status_code != 200:
        raise BadGatewayError(f"Prod {what} API returned HTTP {response.status_code}; nothing was saved")
    try:
        return response.json()
    except ValueError as exc:
        raise BadGatewayError(f"Prod {what} API returned a non-JSON response; nothing was saved") from exc


def calls_from_payload(payload: Any) -> list[dict[str, Any]]:
    """The call records inside a prod report-API response (`{"records": [...]}` or a bare list)."""
    records = payload.get("records") if isinstance(payload, dict) else payload
    records = [r for r in (records or []) if isinstance(r, dict)] if isinstance(records, list) else []
    if not records:
        raise UnprocessableEntityError("Prod report API returned no calls for this phone number")
    return records


def customer_from_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not payload:
        raise UnprocessableEntityError("Prod customer API returned no data for this phone number")
    return payload


async def fetch_prod_calls_payload(phone_number: str, flow_id: str) -> Any:
    """The report API's full JSON response, untouched."""
    return await _fetch(REPORT_PATH, phone_number, flow_id, "report")


async def fetch_prod_customer_payload(phone_number: str, flow_id: str) -> Any:
    return await _fetch(CUSTOMER_PATH, phone_number, flow_id, "customer")


async def fetch_prod_calls(phone_number: str, flow_id: str) -> list[dict[str, Any]]:
    return calls_from_payload(await fetch_prod_calls_payload(phone_number, flow_id))


async def fetch_prod_customer(phone_number: str, flow_id: str) -> dict[str, Any]:
    return customer_from_payload(await fetch_prod_customer_payload(phone_number, flow_id))


async def fetch_full_data(*, phone_number: str, flow_id: str) -> dict:
    """Both prod APIs' full responses for one number, nothing saved. Each must answer HTTP 200 (else BadGatewayError)."""
    return {
        "calls_response": await fetch_prod_calls_payload(phone_number, flow_id),
        "customer_response": await fetch_prod_customer_payload(phone_number, flow_id),
    }


async def preview_from_prod(*, phone_number: str, flow_id: str, with_customer: bool) -> dict:
    """Fetches (without saving anything) what an import would store: the calls API's full response and,
    if the project has a customer-details collection, the customer API's full response."""
    calls_response = await fetch_prod_calls_payload(phone_number, flow_id)
    customer_response: Any = None
    customer_error: str | None = None
    if with_customer:
        try:
            customer_response = await fetch_prod_customer_payload(phone_number, flow_id)
        except BadGatewayError as exc:
            customer_error = str(exc)
    return {"calls_response": calls_response, "customer_response": customer_response, "customer_error": customer_error}


def _meta(flow_id: str, user_id: str, **extra: Any) -> dict[str, Any]:
    return {
        **extra,
        "flow_id": flow_id,
        "source": "prod",
        "imported_at": datetime.now(timezone.utc),
        "imported_by": user_id,
    }


async def import_from_prod(
    client: AsyncIOMotorClient,
    *,
    phone_number: str,
    flow_id: str,
    db_name: str,
    collection_name: str,
    customer_collection_name: str | None,
    user_id: str,
    calls_payload: Any = None,
    customer_payload: Any = None,
) -> dict:
    """Fetches the customer's calls and customer record, then upserts them.
    Calls are keyed on `_import_meta.conversation_id`, the customer record on
    `_import_meta.phone_number` + flow_id, so re-importing replaces earlier
    copies instead of duplicating. The calls must come back 200 with data or
    nothing is written; the customer record is saved only when the project has
    a customer-details collection and its API answered 200."""

    validate_db_name(db_name)
    # `calls_payload` / `customer_payload` = the (possibly edited) responses the admin reviewed in the preview.
    calls = calls_from_payload(calls_payload) if calls_payload is not None else await fetch_prod_calls(phone_number, flow_id)

    customer: dict[str, Any] | None = None
    customer_error: str | None = None
    if customer_collection_name:
        try:
            customer = (
                customer_from_payload(customer_payload)
                if customer_payload is not None
                else await fetch_prod_customer(phone_number, flow_id)
            )
        except (BadGatewayError, UnprocessableEntityError) as exc:
            customer_error = str(exc)

    db = client[db_name]
    created = replaced = skipped = 0
    for record in calls:
        conversation_id = str(record.get("conversation_id") or record.get("call_uid") or "")
        if not conversation_id:
            skipped += 1
            continue
        document = {**record, "_import_meta": _meta(flow_id, user_id, conversation_id=conversation_id)}
        result = await db[collection_name].replace_one(
            {"_import_meta.conversation_id": conversation_id}, document, upsert=True
        )
        if result.upserted_id is not None:
            created += 1
        else:
            replaced += 1

    customer_saved: bool | None = None  # None = not attempted (no collection configured)
    if customer_collection_name:
        customer_saved = customer is not None
        if customer is not None:
            await db[customer_collection_name].replace_one(
                {"_import_meta.phone_number": phone_number, "_import_meta.flow_id": flow_id},
                {**customer, "_import_meta": _meta(flow_id, user_id, phone_number=phone_number)},
                upsert=True,
            )

    logger.info(
        "prod import: user=%s calls created=%d replaced=%d skipped=%d customer=%s -> %s.%s",
        user_id, created, replaced, skipped, customer_saved, db_name, collection_name,
    )
    return {
        "db_name": db_name,
        "collection_name": collection_name,
        "calls_found": len(calls),
        "calls_created": created,
        "calls_replaced": replaced,
        "calls_skipped": skipped,
        "customer_collection_name": customer_collection_name,
        "customer_saved": customer_saved,
        "customer_error": customer_error,
    }
