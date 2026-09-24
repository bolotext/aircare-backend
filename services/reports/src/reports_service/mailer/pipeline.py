"""The single send pipeline: both the scheduler and the manual `/run` route
call this — never two separate implementations — so a manual re-run can
never compute or render a report any differently than the scheduled one did.
"""

from datetime import datetime, timezone
from uuid import uuid4

import httpx

from reports_service.calls.repository import CallFilter
from reports_service.core.deps import resolve_collection_for_project
from reports_service.mailer.render import render_report_html
from reports_service.mailer.send_mail import send_mail
from reports_service.mailer.window import run_key_date_component
from reports_service.metrics.service import compute_metrics
from reports_service.reports import repository as report_repo
from reports_service.reports.model import ReportRun, ReportRunTrigger


def _scope_to_project_ids(scope: str) -> list[str] | None:
    return None if scope == "all" else [scope]


def _scope_label(scope: str) -> str:
    return "All projects" if scope == "all" else f"Project {scope}"


async def run_report(
    *,
    scope: str,
    window_start: datetime,
    window_end: datetime,
    recipients: list[str],
    triggered_by: ReportRunTrigger,
    # Optional: the caller's shared httpx client to the Users service, used
    # to resolve a single-project scope's own configured report
    # db/collection (see core.deps.resolve_collection_for_project) instead of
    # the fixed shared `calls` collection. None (the default, and what every
    # existing caller that doesn't pass one gets) falls back to that fixed
    # collection, same as before; scope="all" always does too, since a
    # combined multi-project report has no single project db/collection to
    # resolve.
    users_http_client: httpx.AsyncClient | None = None,
) -> ReportRun:
    date_component = run_key_date_component(window_start)
    if triggered_by == "scheduled":
        run_key = f"scheduled:{scope}:{date_component}"
    else:
        # Manual runs always get a fresh key: re-running a day's report on
        # request is explicitly allowed and must never be blocked by that
        # day's (or another manual run's) idempotency key.
        run_key = f"manual:{scope}:{date_component}:{uuid4()}"

    run = await report_repo.create_report_run(
        run_key=run_key,
        scope=scope,
        window_start=window_start,
        window_end=window_end,
        recipients=recipients,
        triggered_by=triggered_by,
    )
    if run is None:
        # A scheduled run for this scope+day already exists (unique run_key
        # collision) — a routine duplicate, not an error. Nothing is
        # (re-)sent, and nothing new is persisted: the original run under
        # this run_key already IS the history. This unsaved instance is only
        # a signal back to the caller.
        return ReportRun(
            run_key=run_key,
            scope=scope,
            window_start=window_start,
            window_end=window_end,
            recipients=recipients,
            status="skipped_duplicate",
            triggered_by=triggered_by,
        )

    run = await report_repo.mark_status(run, "running")

    collection = None
    if scope != "all" and users_http_client is not None:
        collection = await resolve_collection_for_project(users_http_client, scope)

    call_filter = CallFilter(project_ids=_scope_to_project_ids(scope), date_from=window_start, date_to=window_end)
    metrics = await compute_metrics(call_filter, collection=collection)
    html = render_report_html(
        scope_label=_scope_label(scope), window_start=window_start, window_end=window_end, metrics=metrics
    )

    date_label = window_start.astimezone(timezone.utc).date().isoformat()
    try:
        await send_mail(
            to_addrs=recipients,
            subject=f"AirCare Call Report — {_scope_label(scope)} — {date_label}",
            message=html,
            is_html=True,
        )
    except Exception as exc:
        return await report_repo.mark_status(run, "failed", attempts=1, errors=[str(exc)])

    return await report_repo.mark_status(run, "success", attempts=1)
