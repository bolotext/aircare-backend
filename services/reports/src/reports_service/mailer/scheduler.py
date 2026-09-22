"""Daily scheduler loop. Documented simplification: enumerating "every
project" lives in the Users service, which has no internal listing endpoint
for an unauthenticated service-to-service caller (only /internal/introspect,
which resolves a *specific user's* access, not "all projects that exist").
Rather than inventing a second internal endpoint on a service this build
isn't touching, the set of scopes this scheduler emails daily is
config-driven (`REPORT_SCOPES`, comma-separated project ids or the literal
"all") — see core/config.py.
"""

import asyncio
import logging
from datetime import datetime

import httpx

from reports_service.core.config import get_settings
from reports_service.mailer.pipeline import run_report
from reports_service.mailer.window import IST, previous_ist_calendar_day
from reports_service.reports import repository as report_repo

logger = logging.getLogger(__name__)


def _parse_scopes(raw: str) -> list[str]:
    return [scope.strip() for scope in raw.split(",") if scope.strip()]


async def _recipients_for_scope(scope: str) -> list[str]:
    project_id = None if scope == "all" else scope
    recipients = await report_repo.list_recipients(project_id)
    return sorted({r.email for r in recipients})


async def run_due_scopes_once(users_http_client: httpx.AsyncClient) -> None:
    settings = get_settings()
    window_start, window_end = previous_ist_calendar_day()
    for scope in _parse_scopes(settings.report_scopes):
        recipients = await _recipients_for_scope(scope)
        if not recipients:
            logger.info("Scope=%s has no configured recipients; skipping today's scheduled send", scope)
            continue
        run = await run_report(
            scope=scope,
            window_start=window_start,
            window_end=window_end,
            recipients=recipients,
            triggered_by="scheduled",
            users_http_client=users_http_client,
        )
        logger.info("Scheduled report run scope=%s status=%s run_key=%s", scope, run.status, run.run_key)


async def run_scheduler_loop(users_http_client: httpx.AsyncClient) -> None:
    """Wakes up every `scheduler_poll_seconds` and fires once per IST
    calendar day at `report_send_hour_ist`. Idempotency against firing twice
    within the same hour-long window is handled by `run_report`'s unique
    run_key, not by this loop's own bookkeeping — `last_sent_date` here is
    only an optimization to skip the (cheap) no-op re-check, not the actual
    duplicate-prevention mechanism.

    `users_http_client` is this process's own connection to the Users
    service (this loop runs as a standalone process — see
    reports_service.__init__._schedule — so it doesn't share the API
    server's app.state.users_http_client), used to resolve each scope's own
    configured report db/collection (see core.deps.resolve_collection_for_project).
    """

    settings = get_settings()
    last_sent_date: str | None = None
    logger.info(
        "Automailer scheduler started: send_hour_ist=%s poll_seconds=%s scopes=%s",
        settings.report_send_hour_ist,
        settings.scheduler_poll_seconds,
        settings.report_scopes,
    )
    while True:
        now_ist = datetime.now(IST)
        today_key = now_ist.date().isoformat()
        if now_ist.hour == settings.report_send_hour_ist and last_sent_date != today_key:
            await run_due_scopes_once(users_http_client)
            last_sent_date = today_key
        await asyncio.sleep(settings.scheduler_poll_seconds)
