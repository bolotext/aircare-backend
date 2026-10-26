from datetime import date

from common.openapi import FORBIDDEN_EXAMPLE, UNAUTHORIZED_EXAMPLE, success_and_error_responses
from common.responses import success_response
from common.schemas import SuccessResponse
from fastapi import APIRouter, Depends, Query

from reports_service.calls import repository as call_repo
from reports_service.calls.repository import CallFilter, day_range_bounds
from reports_service.core.deps import ProjectCallSource, ensure_project_access, require_permission, resolve_project_source
from reports_service.core.users_client import IdentityContext
from reports_service.metrics.collection_stats import CollectionStats, compute_collection_stats
from reports_service.metrics.schema import DashboardMetrics
from reports_service.metrics.service import compute_metrics

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get(
    "",
    response_model=SuccessResponse[DashboardMetrics],
    summary="Get aggregate call metrics for a project",
    description="""Computes volume, answer rate, disposition/language breakdowns, tool-failure counts, and
the scenario pass rate for a project, over the same filters `GET /calls` accepts.

**When to use:** rendering the reporting dashboard's summary tiles/charts.

**Required inputs:** `project_id`. All other query parameters are optional filters, combined with AND —
identical semantics to `GET /calls`, so the two views always reconcile.

**Processing behavior:** every metric definition is fixed and documented on `DashboardMetrics` — a ratio
metric is always `null` (never `0`) when its denominator is zero. Requires the `report.view` permission and
access to `project_id`.

**Errors:** 403 if the caller isn't permitted to view `project_id`; 422 if `project_id` is missing or a
date isn't valid `YYYY-MM-DD`.""",
    responses=success_and_error_responses(
        "Aggregate metrics",
        DashboardMetrics.model_config["json_schema_extra"]["example"],
        UNAUTHORIZED_EXAMPLE,
        FORBIDDEN_EXAMPLE,
    ),
)
async def get_dashboard(
    project_id: str = Query(..., description="The project (Mongo ObjectId) to compute metrics for.", examples=["6aae5deae9a48c4dc62aa398"]),
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
    identity: IdentityContext = Depends(require_permission("report.view")),
    source: ProjectCallSource = Depends(resolve_project_source),
) -> dict:
    # project_id is required: each project's dashboard now reads from that
    # project's own configured report db/collection (see
    # core.deps.resolve_project_source), so there is no longer a single
    # "no filter" collection every project's data can be aggregated from.
    ensure_project_access(identity, project_id)
    window_start, window_end = day_range_bounds(date_from, date_to)
    call_filter = CallFilter(
        project_ids=[project_id],
        date_from=window_start,
        date_to=window_end,
        language=language,
        disposition=disposition,
    )
    metrics = await compute_metrics(
        call_filter, collection=source.collection, adapter=source.adapter, organization_id=source.organization_id
    )
    return success_response(data=metrics)


@router.get(
    "/collection",
    response_model=SuccessResponse[CollectionStats],
    summary="Get funnel, trend, hourly and duration aggregates for a project",
    description="""Computes the calls funnel (dialled → connected → engaged → positive disposition), a daily
trend, per-hour connectivity, disposition/status breakdowns and call-duration buckets over the same filters
`GET /calls` accepts. Definitions are documented in `metrics/collection_stats.py`; days/hours are UTC.

**Errors:** 403 if the caller isn't permitted to view `project_id`; 422 on an invalid date.""",
    responses=success_and_error_responses("Collection aggregates", None, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE),
)
async def get_collection_stats(
    project_id: str = Query(..., description="The project (Mongo ObjectId) to compute for.", examples=["6aae5deae9a48c4dc62aa398"]),
    date_from: date | None = Query(default=None, description="Calls starting on/after this date, inclusive."),
    date_to: date | None = Query(default=None, description="Calls starting on/before this date, inclusive."),
    language: str | None = Query(default=None, description="Exact-match language filter."),
    disposition: str | None = Query(default=None, description="Exact-match disposition filter."),
    call_status: str | None = Query(default=None, description="Exact-match call status filter."),
    engaged_min_seconds: int = Query(default=15, ge=0, le=3600, description="Minimum connected seconds for a call to count as engaged."),
    identity: IdentityContext = Depends(require_permission("report.view")),
    source: ProjectCallSource = Depends(resolve_project_source),
) -> dict:
    ensure_project_access(identity, project_id)
    window_start, window_end = day_range_bounds(date_from, date_to)
    call_filter = CallFilter(
        project_ids=[project_id],
        date_from=window_start,
        date_to=window_end,
        language=language,
        disposition=disposition,
        call_status=call_status,
    )
    calls = await call_repo.list_all(
        call_filter, collection=source.collection, adapter=source.adapter, organization_id=source.organization_id
    )
    return success_response(data=compute_collection_stats(calls, engaged_min_seconds))
