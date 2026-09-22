from datetime import date

from common.errors import NotFoundError
from common.openapi import FORBIDDEN_EXAMPLE, UNAUTHORIZED_EXAMPLE, not_found_example, success_and_error_responses
from common.responses import success_response
from common.schemas import SuccessResponse
from fastapi import APIRouter, Depends, Path, Query

from reports_service.calls import repository as call_repo
from reports_service.calls.model import Call
from reports_service.calls.repository import CallFilter, day_range_bounds
from reports_service.calls.schema import CallDetail, CallListItem, PaginatedCalls
from reports_service.core.config import get_settings
from reports_service.core.deps import ProjectCallSource, ensure_project_access, require_permission, resolve_project_source
from reports_service.core.users_client import IdentityContext

router = APIRouter(prefix="/calls", tags=["calls"])

_LIST_EXAMPLE = {
    "items": [CallListItem.model_config["json_schema_extra"]["example"]],
    "page": 1,
    "page_size": 20,
    "total": 137,
}
_DETAIL_EXAMPLE = CallDetail.model_config["json_schema_extra"]["example"]


def _to_list_item(call: Call) -> CallListItem:
    return CallListItem(
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


def _to_detail(call: Call) -> CallDetail:
    return CallDetail(**_to_list_item(call).model_dump(), organization_id=str(call.organization_id), raw=call.raw)


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
            items=[_to_list_item(c) for c in items], page=page, page_size=effective_page_size, total=total
        )
    )


@router.get(
    "/{conversation_id}",
    response_model=SuccessResponse[CallDetail],
    summary="Get full detail for one call",
    description="""Returns the full record for a single call, including the project-specific `raw` payload
(conversation transcript, event timeline, tool results).

**When to use:** rendering a call-detail view after the caller picked a row from `GET /calls`.

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
    return success_response(data=_to_detail(call))
