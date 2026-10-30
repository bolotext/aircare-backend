"""Copies one call's raw record from the prod conversation API into a
caller-chosen stage Mongo database ("Import from Prod" tab).

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
COLLECTION_NAME_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
FLOW_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
CONVERSATION_ID_PATTERN = r"^[0-9]{6,40}$"


def validate_db_name(db_name: str) -> None:
    if db_name.lower() in RESERVED_DB_NAMES:
        raise UnprocessableEntityError(f"'{db_name}' is a reserved Mongo database and can't be written to")
    if not re.match(DB_NAME_PATTERN, db_name):
        raise UnprocessableEntityError("Database name may only contain letters, digits, '_' and '-'")


async def fetch_prod_record(conversation_id: str, flow_id: str) -> dict[str, Any]:
    settings = get_settings()
    if not settings.prod_import_url:
        raise BadGatewayError("Prod import is not configured")
    try:
        async with httpx.AsyncClient(timeout=settings.prod_import_timeout_seconds) as client:
            response = await client.get(
                settings.prod_import_url,
                params={"q1": conversation_id},
                headers={"accept": "*/*", "flow_id": flow_id},
            )
    except httpx.HTTPError as exc:
        raise BadGatewayError("Unable to reach the prod conversation API") from exc

    if response.status_code != 200:
        raise BadGatewayError(f"Prod conversation API returned HTTP {response.status_code}; nothing was saved")
    try:
        payload = response.json()
    except ValueError as exc:
        raise BadGatewayError("Prod conversation API returned a non-JSON response; nothing was saved") from exc
    if not payload:
        raise UnprocessableEntityError("Prod conversation API returned no data for this conversation id")
    return payload if isinstance(payload, dict) else {"data": payload}


async def import_from_prod(
    client: AsyncIOMotorClient,
    *,
    conversation_id: str,
    flow_id: str,
    db_name: str,
    collection_name: str,
    user_id: str,
) -> dict:
    """Fetches the record and upserts it into `db_name.collection_name`,
    keyed on `_import_meta.conversation_id` so re-importing the same call
    replaces the earlier copy instead of duplicating it. Nothing is written
    unless the prod API answered 200 with data."""

    validate_db_name(db_name)
    record = await fetch_prod_record(conversation_id, flow_id)

    document = {
        **record,
        "_import_meta": {
            "conversation_id": conversation_id,
            "flow_id": flow_id,
            "source": "prod",
            "imported_at": datetime.now(timezone.utc),
            "imported_by": user_id,
        },
    }
    collection = client[db_name][collection_name]
    result = await collection.replace_one({"_import_meta.conversation_id": conversation_id}, document, upsert=True)

    created = result.upserted_id is not None
    logger.info(
        "prod import: user=%s conversation_id=%s -> %s.%s (%s)",
        user_id,
        conversation_id,
        db_name,
        collection_name,
        "created" if created else "replaced",
    )
    return {
        "db_name": db_name,
        "collection_name": collection_name,
        "created": created,
        "document_id": str(result.upserted_id) if created else None,
        "top_level_keys": sorted(record.keys()),
    }
