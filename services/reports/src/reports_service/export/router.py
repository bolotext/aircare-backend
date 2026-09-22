from datetime import date

from fastapi import APIRouter, Depends, Query, Response

from common.openapi import FORBIDDEN_EXAMPLE, UNAUTHORIZED_EXAMPLE
from reports_service.calls import repository as call_repo
from reports_service.calls.repository import CallFilter, day_range_bounds
from reports_service.core.deps import ProjectCallSource, ensure_project_access, require_permission, resolve_project_source
from reports_service.core.users_client import IdentityContext
from reports_service.metrics.csv_export import build_csv
from reports_service.metrics.service import compute_metrics

router = APIRouter(tags=["export"])

_CSV_EXAMPLE = (
    "Metric,Value\nTotal calls,30\nAnswered calls,22\nAnswer rate,73.33%\n\n"
    "conversation_id,project_id,language,call_status,disposition,call_start_time,...\n"
    "1789455660779658485,6aae5deae9a48c4dc62aa398,kn-IN,ANSWERED,NEUTRAL,2026-09-15T07:01:01Z,...\n"
)


@router.get(
    "/export.csv",
    summary="Export a project's calls and metrics as CSV",
    description="""Downloads the same aggregate metrics `GET /dashboard` returns, followed by the same call
rows `GET /calls` would list (unpaginated — every matching call), as a single CSV file.

**When to use:** offline analysis, or attaching to a report outside the dashboard UI. This is the same file
the daily automailer emails.

**Required inputs:** `project_id`. Same optional filters as `GET /calls`/`GET /dashboard`.

**Processing behavior:** requires the `report.export` permission specifically — a caller with `report.view`
alone (e.g. the Viewer role) can see the dashboard but not export it. On success the body is `text/csv`
(note: **not** the JSON envelope every other endpoint uses); a failure still returns the standard JSON
error envelope described above, despite the shared HTTP 200.

**Errors:** 403 if the caller lacks `report.export` or access to `project_id`; 422 if `project_id` is
missing or a date isn't valid `YYYY-MM-DD`.""",
    responses={
        200: {
            "description": "CSV file on success; the standard JSON error envelope on failure — same HTTP 200 either way.",
            "content": {
                "text/csv": {"schema": {"type": "string", "format": "binary"}, "example": _CSV_EXAMPLE},
                "application/json": {
                    "examples": {
                        "error_missing_or_invalid_bearer_token": UNAUTHORIZED_EXAMPLE,
                        "error_caller_lacks_the_required_permission": FORBIDDEN_EXAMPLE,
                    }
                },
            },
        }
    },
)
async def export_csv(
    project_id: str = Query(..., description="The project (Mongo ObjectId) to export.", examples=["6aae5deae9a48c4dc62aa398"]),
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
    # Gated on report.export specifically (not report.view): per the seeded
    # role permissions, VIEWER has report.view but not report.export, so
    # this dependency alone is what keeps exports out of Viewer's hands.
    identity: IdentityContext = Depends(require_permission("report.export")),
    # project_id is required, same as /calls and /dashboard: each project's
    # export now reads from that project's own configured report
    # db/collection (see core.deps.resolve_project_source) rather than a
    # single fixed shared collection every project's data could previously
    # be aggregated from.
    source: ProjectCallSource = Depends(resolve_project_source),
) -> Response:
    ensure_project_access(identity, project_id)
    window_start, window_end = day_range_bounds(date_from, date_to)
    call_filter = CallFilter(
        project_ids=[project_id],
        date_from=window_start,
        date_to=window_end,
        language=language,
        disposition=disposition,
    )
    calls = await call_repo.list_all(
        call_filter, collection=source.collection, adapter=source.adapter, organization_id=source.organization_id
    )
    metrics = await compute_metrics(
        call_filter, collection=source.collection, adapter=source.adapter, organization_id=source.organization_id
    )
    csv_bytes = build_csv(calls, metrics)
    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=call_report.csv"},
    )
