from datetime import date, datetime, timezone

from common.errors import BadGatewayError, NotFoundError, UnprocessableEntityError
from common.openapi import FORBIDDEN_EXAMPLE, UNAUTHORIZED_EXAMPLE, not_found_example, success_and_error_responses
from common.responses import success_response
from common.schemas import SuccessResponse
from fastapi import APIRouter, Body, Depends, Path, Query, Request, Response
from pydantic import BaseModel, Field

from reports_service.calls import repository as call_repo
from reports_service.calls.model import Call
from reports_service.calls.recording import fetch_recording_audio, resolve_recording_ref
from reports_service.calls.repository import CallFilter, day_range_bounds
from reports_service.calls.mapped_fields import diff_source_document, extract_fields, mapped_columns, source_document
from reports_service.calls.journey import load_journey
from reports_service.calls.prod_import import PHONE_NUMBER_PATTERN, import_from_prod
from reports_service.calls.schema import CallDetail, CallJourney, CallListItem, PaginatedCalls, ProdImportResult
from reports_service.core.config import get_settings
from reports_service.core.deps import ProjectCallSource, ensure_project_access, require_permission, resolve_project_source
from reports_service.core.users_client import IdentityContext, get_project_config
from reports_service.db.mongodb import get_client

router = APIRouter(prefix="/calls", tags=["calls"])

_LIST_EXAMPLE = {
    "items": [CallListItem.model_config["json_schema_extra"]["example"]],
    "page": 1,
    "page_size": 20,
    "total": 137,
}
_DETAIL_EXAMPLE = CallDetail.model_config["json_schema_extra"]["example"]


def scenario_from_report(call: Call) -> str | None:
    """The scenario stored on the call's own report document — the only place
    scenarios are kept."""

    value = (call._source_doc or {}).get("scenarios")
    return value if isinstance(value, str) and value else None


def _to_list_item(call: Call, field_mapping: dict[str, str] | None = None) -> CallListItem:
    return CallListItem(
        fields=extract_fields(call._source_doc, field_mapping or {}),
        conversation_id=call.conversation_id,
        project_id=str(call.project_id),
        bot_id=call.bot_id,
        language=call.language,
        call_status=call.call_status,
        disposition=call.disposition,
        call_start_time=call.call_start_time,
        call_connected_time=call.call_connected_time,
        call_end_time=call.call_end_time,
        connected_seconds=call.connected_seconds,
        tool_failure_count=call.tool_failure_count,
    )


def _to_detail(call: Call, *, recording_available: bool) -> CallDetail:
    return CallDetail(
        **_to_list_item(call).model_dump(),
        organization_id=str(call.organization_id),
        raw=call.raw,
        source_document=source_document(call._source_doc),
        recording_available=recording_available,
    )


@router.get(
    "",
    response_model=SuccessResponse[PaginatedCalls],
    summary="List calls for a project",
    description="""Returns a paginated, most-recent-first list of call records for one project.

**When to use:** populating a call log / call list table, or scanning a date range before drilling into
`GET /calls/{conversation_id}` for full detail.

**Required inputs:** `project_id`. All other query parameters are optional filters.

**Processing behavior:** filters are combined with AND. `date_from`/`date_to` are calendar dates
(inclusive) applied to `call_start_time`; omitting one leaves that side of the range open. Requires the
`report.view` permission and access to `project_id`.

**Errors:** 403 if the caller isn't permitted to view `project_id`; 422 if `project_id` is missing or a
date isn't valid `YYYY-MM-DD`.""",
    responses=success_and_error_responses(
        "One page of calls", _LIST_EXAMPLE, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE
    ),
)
async def list_calls(
    project_id: str = Query(..., description="The project (Mongo ObjectId) to list calls for.", examples=["6aae5deae9a48c4dc62aa398"]),
    date_from: date | None = Query(
        default=None, description="Only include calls starting on/after this calendar date, inclusive.", examples=["2026-09-01"]
    ),
    date_to: date | None = Query(
        default=None, description="Only include calls starting on/before this calendar date, inclusive.", examples=["2026-09-15"]
    ),
    language: str | None = Query(default=None, description="Exact-match language/locale filter, e.g. `en-IN`.", examples=["en-IN"]),
    disposition: str | None = Query(
        default=None, description="Exact-match disposition filter, e.g. `RESOLVED`, `ABANDONED`.", examples=["RESOLVED"]
    ),
    call_status: str | None = Query(default=None, description="Exact-match call status filter, e.g. `ANSWERED`.", examples=["ANSWERED"]),
    search: str | None = Query(
        default=None, max_length=100, description="Case-insensitive substring match on conversation_id, flow_id or customer phone number.", examples=["17930", "prod-e3fbb647899d"]
    ),
    flow_id: str | None = Query(default=None, max_length=100, description="Exact-match flow_id filter.", examples=["prod-e3fbb647899d"]),
    transcript_search: str | None = Query(
        default=None, min_length=2, max_length=100, description="Case-insensitive substring match on the text of any turn in the call transcript. Each returned item carries a `transcript_match` excerpt.", examples=["inhaler"]
    ),
    page: int = Query(default=1, ge=1, description="1-indexed page number to return."),
    page_size: int | None = Query(
        default=None,
        ge=1,
        description="Items per page. Defaults to the service's configured default and is capped at its "
        "configured maximum, whichever value is requested.",
        examples=[20],
    ),
    identity: IdentityContext = Depends(require_permission("report.view")),
    source: ProjectCallSource = Depends(resolve_project_source),
) -> dict:
    ensure_project_access(identity, project_id)
    settings = get_settings()
    effective_page_size = min(page_size or settings.default_page_size, settings.max_page_size)

    window_start, window_end = day_range_bounds(date_from, date_to)
    call_filter = CallFilter(
        project_ids=[project_id],
        date_from=window_start,
        date_to=window_end,
        language=language,
        disposition=disposition,
        call_status=call_status,
        search=search,
        flow_id=flow_id,
        transcript_search=transcript_search,
    )
    items, total = await call_repo.list_paginated(
        call_filter,
        page=page,
        page_size=effective_page_size,
        collection=source.collection,
        adapter=source.adapter,
        organization_id=source.organization_id,
    )
    return success_response(
        data=PaginatedCalls(
            columns=mapped_columns(source.field_mapping),
            items=[
                _to_list_item(c, source.field_mapping).model_copy(
                    update={
                        "scenario": scenario_from_report(c),
                        "transcript_match": call_repo.transcript_snippet(c.raw, transcript_search) if transcript_search else None,
                    }
                )
                for c in items
            ],
            page=page,
            page_size=effective_page_size,
            total=total,
        )
    )


class ProdImportBody(BaseModel):
    project_id: str = Field(description="The project whose report collection the record is saved into and whose Flow ID is used.")
    phone_number: str = Field(pattern=PHONE_NUMBER_PATTERN, description="Customer phone number (digits only) to fetch prod calls and customer details for.")
    confirm_pii: bool = Field(description="Must be true: the caller confirms this personal data may be copied to the stage database.")


@router.post(
    "/import-from-prod",
    response_model=SuccessResponse[ProdImportResult],
    summary="Import a customer's prod calls and details (by phone number) into a project",
    description="""Looks the customer up by **phone number** on the prod report API — using the project's **Flow ID**
(Settings → Data Source) as the `flow_id` header — and, **only if it answers HTTP 200 with calls**, upserts every
call into the project's report database/collection (re-importing replaces earlier copies, keyed on conversation id).
If the project has a customer-details collection, the prod customer API is fetched too and saved there (keyed on
phone number + flow_id); a failure of that second call doesn't undo the saved calls and is reported in `customer_error`.
Saved documents get an `_import_meta` block (source, flow_id, imported_at, imported_by).

**Privacy:** the data is personal. Admin only (`report.import`), requires `confirm_pii: true`, is audit-logged
(without the phone number), and the records are never returned — only where they were written and counts.

**Errors:** 403 without `report.import`; 404 if the project doesn't exist; 422 if the project has no Flow ID
set, or for a reserved database name or no calls found; 502 if the prod API is unreachable, not configured, or answers non-200 (nothing is saved).""",
    responses=success_and_error_responses("Saved", {}, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE),
)
async def import_call_from_prod(
    request: Request,
    body: ProdImportBody = Body(...),
    identity: IdentityContext = Depends(require_permission("report.import")),
) -> dict:
    if not body.confirm_pii:
        raise UnprocessableEntityError("confirm_pii must be true to copy personal data into the report collection")
    ensure_project_access(identity, body.project_id)
    config = await get_project_config(request.app.state.users_http_client, body.project_id)
    if not config.flow_id:
        raise UnprocessableEntityError("This project has no Flow ID — set it in Settings → Data Source first")
    result = await import_from_prod(
        get_client(),
        phone_number=body.phone_number,
        flow_id=config.flow_id,
        db_name=config.report_db_name,
        collection_name=config.report_collection_name,
        customer_collection_name=config.customer_details_collection_name,
        user_id=identity.user_id,
    )
    return success_response(message="Imported from prod", data=ProdImportResult(**result))


@router.get(
    "/{conversation_id}",
    response_model=SuccessResponse[CallDetail],
    summary="Get full detail for one call",
    description="""Returns the full record for a single call, including the project-specific `raw` payload
(conversation transcript, event timeline, tool results).

**When to use:** rendering a call-detail view after the caller picked a row from `GET /calls`.
`recording_available` tells the caller whether to immediately follow up with
`GET /calls/{conversation_id}/recording` (e.g. to auto-load playback) rather than showing a control that
would just 404.

**Required inputs:** `conversation_id` (path) and `project_id` (query) — both are required because a call's
`conversation_id` alone is not guaranteed unique across every project's configured data source; the pair
scopes the lookup to a single project's data.

**Errors:** 403 if the caller isn't permitted to view `project_id`; 404 if no call with that
`conversation_id` exists within `project_id`'s data.""",
    responses=success_and_error_responses(
        "Call found", _DETAIL_EXAMPLE, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE, not_found_example("Call")
    ),
)
async def get_call(
    conversation_id: str = Path(..., description="The call's unique conversation identifier.", examples=["1789455660779658485"]),
    project_id: str = Query(..., description="The project (Mongo ObjectId) this call belongs to.", examples=["6aae5deae9a48c4dc62aa398"]),
    identity: IdentityContext = Depends(require_permission("report.view")),
    source: ProjectCallSource = Depends(resolve_project_source),
) -> dict:
    ensure_project_access(identity, project_id)
    call = await call_repo.get_by_conversation_id(
        conversation_id,
        project_id,
        collection=source.collection,
        adapter=source.adapter,
        organization_id=source.organization_id,
    )
    if call is None:
        raise NotFoundError("Call not found")
    ref = await resolve_recording_ref(source.collection, source.adapter, project_id, source.organization_id, conversation_id)
    return success_response(data=_to_detail(call, recording_available=ref is not None))


async def _write_scenario_to_report(collection, call: Call, text: str, user_id: str) -> None:
    """Stores the edit on the call's own document in the project's report
    collection (`scenarios`, plus who/when), so the report itself carries it.
    If the report can't be written the request fails."""

    doc_id = (call._source_doc or {}).get("_id")
    if doc_id is None:
        raise NotFoundError("Call not found")
    if text:
        update = {
            "$set": {
                "scenarios": text,
                "scenarios_updated_by": user_id,
                "scenarios_updated_at": datetime.now(timezone.utc),
            }
        }
    else:
        update = {"$unset": {"scenarios": "", "scenarios_updated_by": "", "scenarios_updated_at": ""}}
    try:
        await collection.update_one({"_id": doc_id}, update)
    except Exception as exc:
        raise BadGatewayError("Unable to save to the report collection") from exc


class CallLookupBody(BaseModel):
    conversation_ids: list[str] = Field(min_length=1, max_length=50, description="Conversation ids to fetch, in order.")


@router.post(
    "/lookup",
    response_model=SuccessResponse[list[CallListItem]],
    summary="Fetch specific calls by conversation id",
    description="""Returns the current list-view record (including `scenario`) for each requested
conversation id in a project, in the order requested; ids that don't exist are skipped.

**When to use:** showing the latest data for calls a user has selected across several pages.

**Errors:** 403 without `report.view`/project access.""",
    responses=success_and_error_responses("The calls", [], UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE),
)
async def lookup_calls(
    project_id: str = Query(..., description="The project (Mongo ObjectId) the calls belong to."),
    body: CallLookupBody = Body(...),
    identity: IdentityContext = Depends(require_permission("report.view")),
    source: ProjectCallSource = Depends(resolve_project_source),
) -> dict:
    ensure_project_access(identity, project_id)
    calls = await call_repo.get_many_by_conversation_ids(
        list(dict.fromkeys(body.conversation_ids)),
        project_id,
        collection=source.collection,
        adapter=source.adapter,
        organization_id=source.organization_id,
    )
    return success_response(
        data=[
            _to_list_item(c, source.field_mapping).model_copy(update={"scenario": scenario_from_report(c)})
            for c in calls
        ]
    )


MAX_BULK_SOURCE_DOCUMENTS = 5000


class SourceDocumentsBody(BaseModel):
    conversation_ids: list[str] = Field(
        min_length=1, max_length=MAX_BULK_SOURCE_DOCUMENTS, description="Conversation ids to fetch, in order."
    )


@router.post(
    "/source-documents",
    response_model=SuccessResponse[list[dict]],
    summary="Fetch the full source documents of many calls",
    description=f"""Returns each requested call's complete `source_document`, in the order requested; ids that don't
exist are skipped. At most {MAX_BULK_SOURCE_DOCUMENTS} ids per request.

**When to use:** the Conversation Logs "Show raw JSON" list for a large selection.

**Errors:** 403 without `report.view`/project access; 422 over {MAX_BULK_SOURCE_DOCUMENTS} ids.""",
    responses=success_and_error_responses("The documents", [], UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE),
)
async def bulk_source_documents(
    project_id: str = Query(..., description="The project (Mongo ObjectId) the calls belong to."),
    body: SourceDocumentsBody = Body(...),
    identity: IdentityContext = Depends(require_permission("report.view")),
    source: ProjectCallSource = Depends(resolve_project_source),
) -> dict:
    ensure_project_access(identity, project_id)
    calls = await call_repo.get_many_by_conversation_ids(
        list(dict.fromkeys(body.conversation_ids)),
        project_id,
        collection=source.collection,
        adapter=source.adapter,
        organization_id=source.organization_id,
    )
    return success_response(data=[source_document(c._source_doc) for c in calls])


class ScenarioBody(BaseModel):
    scenario: str = Field(max_length=500, description="The scenario text; an empty string clears it.")


@router.put(
    "/{conversation_id}/scenario",
    response_model=SuccessResponse[ScenarioBody],
    summary="Set the Scenarios text for a call",
    description="""Saves (or clears, with an empty string) the tester-entered Scenarios text for one call. Stored
on the call's document in the project's report collection (`scenarios`, `scenarios_updated_by`,
`scenarios_updated_at`).

**Errors:** 403 without `report.annotate` or project access; 404 if the call isn't in the project; 422 if the
text is over 500 characters.""",
    responses=success_and_error_responses("Saved", {"scenario": "Valid PTP date"}, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE, not_found_example("Call")),
)
async def set_call_scenario(
    conversation_id: str = Path(..., description="The call's conversation identifier."),
    project_id: str = Query(..., description="The project (Mongo ObjectId) this call belongs to."),
    body: ScenarioBody = Body(...),
    identity: IdentityContext = Depends(require_permission("report.annotate")),
    source: ProjectCallSource = Depends(resolve_project_source),
) -> dict:
    ensure_project_access(identity, project_id)
    call = await call_repo.get_by_conversation_id(
        conversation_id,
        project_id,
        collection=source.collection,
        adapter=source.adapter,
        organization_id=source.organization_id,
    )
    if call is None:
        raise NotFoundError("Call not found")
    text = body.scenario.strip()
    await _write_scenario_to_report(source.collection, call, text, identity.user_id)
    return success_response(message="Scenario saved", data=ScenarioBody(scenario=text))


class SourceDocumentBody(BaseModel):
    document: dict = Field(description="The complete edited source document, as returned in `source_document`.")


@router.put(
    "/{conversation_id}/source-document",
    response_model=SuccessResponse[CallDetail],
    summary="Edit a call's raw source document",
    description="""Applies an edited copy of the call's `source_document` to its document in the project's report
collection. Only changed keys are written (`$set`), keys removed from the payload are `$unset`, and
`source_edited_by` / `source_edited_at` record who/when.

**Guards:** `_id`, audit fields and withheld personal-data fields cannot be changed or removed (422), so a
`[withheld]` marker can never overwrite real values.

**Errors:** 403 without `report.annotate` or project access; 404 if the call isn't in the project; 422 for a
forbidden edit.""",
    responses=success_and_error_responses("Saved", _DETAIL_EXAMPLE, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE, not_found_example("Call")),
)
async def set_call_source_document(
    conversation_id: str = Path(..., description="The call's conversation identifier."),
    project_id: str = Query(..., description="The project (Mongo ObjectId) this call belongs to."),
    body: SourceDocumentBody = Body(...),
    identity: IdentityContext = Depends(require_permission("report.annotate")),
    source: ProjectCallSource = Depends(resolve_project_source),
) -> dict:
    ensure_project_access(identity, project_id)
    call = await call_repo.get_by_conversation_id(
        conversation_id,
        project_id,
        collection=source.collection,
        adapter=source.adapter,
        organization_id=source.organization_id,
    )
    if call is None:
        raise NotFoundError("Call not found")
    doc_id = (call._source_doc or {}).get("_id")
    if doc_id is None:
        raise NotFoundError("Call not found")
    try:
        to_set, to_unset = diff_source_document(call._source_doc, body.document)
    except ValueError as exc:
        raise UnprocessableEntityError(str(exc)) from exc
    if to_set or to_unset:
        update: dict = {
            "$set": {**to_set, "source_edited_by": identity.user_id, "source_edited_at": datetime.now(timezone.utc)}
        }
        if to_unset:
            update["$unset"] = {k: "" for k in to_unset}
        try:
            await source.collection.update_one({"_id": doc_id}, update)
        except Exception as exc:
            raise BadGatewayError("Unable to save to the report collection") from exc
    fresh = await call_repo.get_by_conversation_id(
        conversation_id,
        project_id,
        collection=source.collection,
        adapter=source.adapter,
        organization_id=source.organization_id,
    )
    ref = await resolve_recording_ref(source.collection, source.adapter, project_id, source.organization_id, conversation_id)
    return success_response(message="Source document saved", data=_to_detail(fresh or call, recording_available=ref is not None))


@router.get(
    "/{conversation_id}/journey",
    response_model=SuccessResponse[CallJourney],
    summary="Get the full call journey for this call's customer (dev logs)",
    description="""Finds every call placed to the same phone number as this call and returns them oldest-first,
each with the retry-scheduler state it recorded (`sequence_number`, `trigger_call`, `next_trigger_date`,
`call_sequence_mapping`, ...) and a list of detected inconsistencies (duplicate `sequence_number`, a call
placed before the previous call's `next_trigger_date`, a call after `trigger_call=false`, a
`call_sequence_mapping` count that disagrees with the logs, ...).

**Privacy:** the phone number is used only server-side to link calls and is never returned — only a masked
last-4 hint. Personal-data fields are excluded from every entry.

**Errors:** 403 without `report.view`/project access; 404 if the call isn't in the project. If the call's
source record has no phone number the response is 200 with `available: false` and a `reason`.""",
    responses=success_and_error_responses("The customer's call journey", {}, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE, not_found_example("Call")),
)
async def get_call_journey(
    conversation_id: str = Path(..., description="Any call in the journey.", examples=["1789455660779658485"]),
    project_id: str = Query(..., description="The project (Mongo ObjectId) this call belongs to."),
    identity: IdentityContext = Depends(require_permission("report.view")),
    source: ProjectCallSource = Depends(resolve_project_source),
) -> dict:
    ensure_project_access(identity, project_id)
    if source.adapter is not None:
        found = await call_repo.get_with_raw_source_by_conversation_id(
            conversation_id,
            project_id,
            collection=source.collection,
            adapter=source.adapter,
            organization_id=source.organization_id,
        )
        source_doc = found[1] if found else None
    else:
        call = await call_repo.get_by_conversation_id(conversation_id, project_id, collection=source.collection)
        source_doc = call._source_doc if call else None
    if source_doc is None:
        raise NotFoundError("Call not found")
    journey = await load_journey(
        source.collection,
        source_doc,
        adapter=source.adapter,
        project_id=project_id,
        organization_id=source.organization_id,
    )
    return success_response(data=CallJourney(**journey))


@router.get(
    "/{conversation_id}/recording",
    summary="Stream a call's audio recording",
    description="""Proxies this call's recording from the internal audio server — the caller's browser never
    talks to that (unauthenticated, private-network-only) service directly; this service's own project RBAC
    is the only access control a recording gets.

    **When to use:** a "play recording" control on the call-detail view, after `GET /calls/{conversation_id}`.

    **Processing behavior:** returns raw audio bytes on success (`Content-Type` as reported by the audio
    server, `audio/wav` if it doesn't say). Only available for a project whose configured source is a raw CDR
    export (report_llm/call_logs/aircare_calls-shaped) — seed/demo data and any other native-Call-schema
    project has no known recording. Requires `report.view` and access to `project_id`, same as call detail.

    **Errors:** 403 if the caller isn't permitted to view `project_id`; 404 if the call doesn't exist or has
    no known recording; 502 if the internal audio server is unreachable or errors.""",
    responses={
        200: {
            "description": "Recording audio bytes.",
            "content": {"audio/wav": {"schema": {"type": "string", "format": "binary"}}},
        },
        401: {"content": {"application/json": {"example": UNAUTHORIZED_EXAMPLE}}},
        403: {"content": {"application/json": {"example": FORBIDDEN_EXAMPLE}}},
        404: {"content": {"application/json": {"example": not_found_example("Call")}}},
    },
)
async def get_call_recording(
    conversation_id: str = Path(..., description="The call's unique conversation identifier.", examples=["1789455660779658485"]),
    project_id: str = Query(..., description="The project (Mongo ObjectId) this call belongs to.", examples=["6aae5deae9a48c4dc62aa398"]),
    identity: IdentityContext = Depends(require_permission("report.view")),
    source: ProjectCallSource = Depends(resolve_project_source),
) -> Response:
    ensure_project_access(identity, project_id)
    ref = await resolve_recording_ref(source.collection, source.adapter, project_id, source.organization_id, conversation_id)
    if ref is None:
        raise NotFoundError("No recording available for this call")
    audio_bytes, content_type = await fetch_recording_audio(ref)
    return Response(content=audio_bytes, media_type=content_type)
