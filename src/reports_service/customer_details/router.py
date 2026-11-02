"""Customer-details CRUD against the project's configured customer-details
collection (Project.customer_details_collection_name, in its report db).

Records are PII: every route re-checks `ensure_project_access`, requires a
dedicated `customer_detail.*` permission (not granted to Viewers), and never
logs field values. A record is a flat Mongo document; the Field Mapper
(Project.customer_details_fields) decides which keys exist and their types.
The configured collection itself is the scope — it carries no project_id.
"""

import re

from bson import ObjectId
from common.errors import BadRequestError, NotFoundError, UnprocessableEntityError
from common.openapi import FORBIDDEN_EXAMPLE, UNAUTHORIZED_EXAMPLE, success_and_error_responses
from common.responses import success_response
from common.schemas import SuccessResponse
from fastapi import APIRouter, Body, Depends, Path, Query, Request
from pydantic import BaseModel, Field

from reports_service.core.config import get_settings
from reports_service.core.deps import ensure_project_access, require_permission
from reports_service.core.users_client import IdentityContext, get_project_config
from reports_service.customer_details.service import diff_document, full_document, normalize_type, serialize, validate_fields
from reports_service.db.mongodb import get_client

router = APIRouter(prefix="/customer-details", tags=["customer-details"])


class CustomerRecordOut(BaseModel):
    id: str = Field(description="Mongo `_id` of the record.")
    fields: dict = Field(description="Values keyed by the project's configured customer fields.")
    other_fields: dict = Field(default_factory=dict, description="Single-record view only: every other key in the stored document.")
    document: dict | None = Field(default=None, description="Single-record view only: the complete stored document (JSON-safe, `_id` included).")


class PaginatedCustomerRecords(BaseModel):
    fields: list[dict] = Field(description="The project's configured field schema (`key`, Mongo `type` alias, optional `options`).")
    items: list[CustomerRecordOut]
    page: int
    page_size: int
    total: int


class CustomerDocumentBody(BaseModel):
    document: dict = Field(description="The complete edited document, as returned in `document`.")


class CustomerRecordBody(BaseModel):
    fields: dict = Field(description="Values keyed by configured customer field. Unknown keys are rejected.")


async def _resolve(request: Request, project_id: str):
    config = await get_project_config(request.app.state.users_http_client, project_id)
    if not config.customer_details_collection_name or not config.customer_details_fields:
        raise UnprocessableEntityError(
            "Customer details aren't configured for this project: set a customer details collection "
            "and at least one customer field in the project's settings."
        )
    collection = get_client()[config.report_db_name][config.customer_details_collection_name]
    return collection, config.customer_details_fields


def _object_id(value: str) -> ObjectId:
    try:
        return ObjectId(value)
    except Exception:
        raise BadRequestError("record id is not valid", {"field": "record_id"})


@router.get(
    "",
    response_model=SuccessResponse[PaginatedCustomerRecords],
    summary="List a project's customer-details records",
    description="""Paginated, newest-first list. `search` is a case-insensitive substring match across the
project's string-typed fields.

**Errors:** 403 without `customer_detail.view`/project access; 422 if the project has no customer-details
collection or fields configured.""",
    responses=success_and_error_responses("One page of records", None, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE),
)
async def list_records(
    request: Request,
    project_id: str = Query(..., description="The project (Mongo ObjectId)."),
    search: str | None = Query(default=None, max_length=100, description="Substring match across text fields."),
    page: int = Query(default=1, ge=1),
    page_size: int | None = Query(default=None, ge=1),
    identity: IdentityContext = Depends(require_permission("customer_detail.view")),
) -> dict:
    ensure_project_access(identity, project_id)
    collection, schema = await _resolve(request, project_id)
    settings = get_settings()
    size = min(page_size or settings.default_page_size, settings.max_page_size)

    query: dict = {}
    text_keys = [f["key"] for f in schema if normalize_type(f["type"]) == "string"]
    if search and text_keys:
        pattern = {"$regex": re.escape(search), "$options": "i"}
        query = {"$or": [{key: pattern} for key in text_keys]}

    total = await collection.count_documents(query)
    cursor = collection.find(query).sort("_id", -1).skip((page - 1) * size).limit(size)
    docs = await cursor.to_list(size)
    return success_response(
        data=PaginatedCustomerRecords(
            fields=[{**f, "type": normalize_type(f["type"])} for f in schema], items=[serialize(d, schema) for d in docs], page=page, page_size=size, total=total
        )
    )


@router.post(
    "",
    response_model=SuccessResponse[CustomerRecordOut],
    summary="Create a customer-details record",
    description="**Errors:** 403 without `customer_detail.create`/project access; 422 if a value doesn't match its field type or the key isn't configured.",
    responses=success_and_error_responses("Record created", None, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE),
)
async def create_record(
    request: Request,
    project_id: str = Query(..., description="The project (Mongo ObjectId)."),
    body: CustomerRecordBody = Body(...),
    identity: IdentityContext = Depends(require_permission("customer_detail.create")),
) -> dict:
    ensure_project_access(identity, project_id)
    collection, schema = await _resolve(request, project_id)
    clean = validate_fields(body.fields, schema)
    if not clean:
        raise UnprocessableEntityError("Provide at least one field")
    result = await collection.insert_one(clean)
    doc = await collection.find_one({"_id": result.inserted_id})
    return success_response(code=201, message="Record created", data=serialize(doc, schema))


@router.get(
    "/{record_id}",
    response_model=SuccessResponse[CustomerRecordOut],
    summary="Get one customer-details record",
    responses=success_and_error_responses("Record", None, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE),
)
async def get_record(
    request: Request,
    record_id: str = Path(...),
    project_id: str = Query(..., description="The project (Mongo ObjectId)."),
    identity: IdentityContext = Depends(require_permission("customer_detail.view")),
) -> dict:
    ensure_project_access(identity, project_id)
    collection, schema = await _resolve(request, project_id)
    doc = await collection.find_one({"_id": _object_id(record_id)})
    if doc is None:
        raise NotFoundError("Record not found")
    out = serialize(doc, schema, include_other=True)
    out["document"] = full_document(doc)
    return success_response(data=out)


@router.patch(
    "/{record_id}",
    response_model=SuccessResponse[CustomerRecordOut],
    summary="Update a customer-details record",
    description="Only the supplied keys change; `null` clears a value.",
    responses=success_and_error_responses("Record updated", None, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE),
)
async def update_record(
    request: Request,
    record_id: str = Path(...),
    project_id: str = Query(..., description="The project (Mongo ObjectId)."),
    body: CustomerRecordBody = Body(...),
    identity: IdentityContext = Depends(require_permission("customer_detail.update")),
) -> dict:
    ensure_project_access(identity, project_id)
    collection, schema = await _resolve(request, project_id)
    clean = validate_fields(body.fields, schema)
    if not clean:
        raise UnprocessableEntityError("Provide at least one field")
    doc = await collection.find_one_and_update(
        {"_id": _object_id(record_id)}, {"$set": clean}, return_document=True
    )
    if doc is None:
        raise NotFoundError("Record not found")
    return success_response(message="Record updated", data=serialize(doc, schema))


@router.put(
    "/{record_id}/document",
    response_model=SuccessResponse[CustomerRecordOut],
    summary="Edit a customer-details record's full raw document",
    description="""Applies an edited copy of the whole stored document. Only changed keys are written; keys removed
from the payload are unset. Changed configured fields are validated against their declared type; other keys are
stored as given. `_id` cannot be changed or removed.

**Errors:** 403 without `customer_detail.update`/project access; 404 if missing; 422 for an invalid edit.""",
    responses=success_and_error_responses("Record updated", None, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE),
)
async def update_document(
    request: Request,
    record_id: str = Path(...),
    project_id: str = Query(..., description="The project (Mongo ObjectId)."),
    body: CustomerDocumentBody = Body(...),
    identity: IdentityContext = Depends(require_permission("customer_detail.update")),
) -> dict:
    ensure_project_access(identity, project_id)
    collection, schema = await _resolve(request, project_id)
    oid = _object_id(record_id)
    current = await collection.find_one({"_id": oid})
    if current is None:
        raise NotFoundError("Record not found")
    to_set, to_unset = diff_document(current, body.document, schema)
    update: dict = {}
    if to_set:
        update["$set"] = to_set
    if to_unset:
        update["$unset"] = {k: "" for k in to_unset}
    doc = (
        await collection.find_one_and_update({"_id": oid}, update, return_document=True) if update else current
    )
    out = serialize(doc, schema, include_other=True)
    out["document"] = full_document(doc)
    return success_response(message="Record updated", data=out)


@router.delete(
    "/{record_id}",
    response_model=SuccessResponse[None],
    summary="Delete a customer-details record",
    responses=success_and_error_responses("Record deleted", None, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE),
)
async def delete_record(
    request: Request,
    record_id: str = Path(...),
    project_id: str = Query(..., description="The project (Mongo ObjectId)."),
    identity: IdentityContext = Depends(require_permission("customer_detail.delete")),
) -> dict:
    ensure_project_access(identity, project_id)
    collection, _ = await _resolve(request, project_id)
    result = await collection.delete_one({"_id": _object_id(record_id)})
    if result.deleted_count == 0:
        raise NotFoundError("Record not found")
    return success_response(message="Record deleted", data=None)
