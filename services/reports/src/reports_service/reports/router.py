"""Recipient configuration + manual/automated report-run visibility.
Permission-gated the same way as the rest of the service (report.*
permission strings resolved via the Users service's introspect response),
plus explicit project-access checks — see the ADMIN-only "global recipient"
and "all projects run history" guards below, which the permission string
alone does not express.
"""

from common.errors import BadRequestError, ForbiddenError, NotFoundError
from common.openapi import (
    FORBIDDEN_EXAMPLE,
    UNAUTHORIZED_EXAMPLE,
    error_example,
    not_found_example,
    success_and_error_responses,
)
from common.responses import success_response
from common.schemas import SuccessResponse
from fastapi import APIRouter, Depends, Path, Query, Request

from reports_service.core.deps import ensure_project_access, require_permission
from reports_service.core.users_client import IdentityContext
from reports_service.mailer.pipeline import run_report
from reports_service.mailer.window import previous_ist_calendar_day
from reports_service.reports import repository as report_repo
from reports_service.reports.model import ReportRecipient, ReportRun
from reports_service.reports.schema import ManualRunRequest, RecipientCreate, RecipientOut, ReportRunOut

router = APIRouter(tags=["automailer"])

_RECIPIENT_EXAMPLE = {"id": "6aae5e2f12975e28491efb4a", "project_id": "6aae5deae9a48c4dc62aa398", "email": "qa-test@example.com"}
_RUN_EXAMPLE = ReportRunOut.model_config["json_schema_extra"]["example"]
_ADMIN_ONLY_EXAMPLE = error_example("Caller isn't an Admin", 403, "Only an Admin may view the global recipient list", "FORBIDDEN")


def _to_recipient_out(recipient: ReportRecipient) -> RecipientOut:
    return RecipientOut(
        id=str(recipient.id), project_id=str(recipient.project_id) if recipient.project_id else None, email=recipient.email
    )


def _to_run_out(run: ReportRun) -> ReportRunOut:
    return ReportRunOut(
        id=str(run.id) if run.id else None,
        run_key=run.run_key,
        scope=run.scope,
        window_start=run.window_start,
        window_end=run.window_end,
        recipients=run.recipients,
        status=run.status,
        attempts=run.attempts,
        errors=run.errors,
        triggered_by=run.triggered_by,
    )


@router.get(
    "/recipients",
    response_model=SuccessResponse[list[RecipientOut]],
    summary="List configured automailer recipients",
    description="""Returns who currently receives the daily report email for a project (or, for an Admin
with no `project_id`, the global recipient list).

**When to use:** rendering the recipient-management screen before adding/removing one.

**Processing behavior:** with `project_id` set, returns that project's own recipients plus any global
recipients (who receive every project's report). Without `project_id`, only an Admin may call this — it
returns the global list. Requires `report.configure_recipients`.

**Errors:** 403 if the caller isn't permitted, isn't assigned to `project_id`, or (when `project_id` is
omitted) isn't an Admin.""",
    responses=success_and_error_responses(
        "Recipients for a project", [_RECIPIENT_EXAMPLE], UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE, _ADMIN_ONLY_EXAMPLE
    ),
)
async def list_recipients(
    project_id: str | None = Query(
        default=None,
        description="Project (Mongo ObjectId) to list recipients for. Omit to list the global recipient list (Admin only).",
        examples=["6aae5deae9a48c4dc62aa398"],
    ),
    identity: IdentityContext = Depends(require_permission("report.configure_recipients")),
) -> dict:
    if project_id is not None:
        ensure_project_access(identity, project_id)
    elif not identity.has_all_projects:
        raise ForbiddenError("Only an Admin may view the global recipient list")
    recipients = await report_repo.list_recipients(project_id)
    return success_response(data=[_to_recipient_out(r) for r in recipients])


@router.post(
    "/recipients",
    response_model=SuccessResponse[RecipientOut],
    summary="Add an automailer recipient",
    description="""Adds an email address to a project's (or, Admin-only, the global) daily report recipient
list.

**When to use:** onboarding a new stakeholder who should receive the daily report email.

**Required inputs:** `email`; `project_id` (or `null` for a global recipient — Admin only).

**Errors:** 403 if the caller isn't permitted, isn't assigned to `project_id`, or (for a global recipient)
isn't an Admin; 422 if `email` is missing.""",
    responses=success_and_error_responses(
        "Recipient added", _RECIPIENT_EXAMPLE, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE
    ),
)
async def add_recipient(
    body: RecipientCreate,
    identity: IdentityContext = Depends(require_permission("report.configure_recipients")),
) -> dict:
    if body.project_id is None:
        if not identity.has_all_projects:
            raise ForbiddenError("Only an Admin may configure a global recipient")
    else:
        ensure_project_access(identity, body.project_id)
    recipient = await report_repo.add_recipient(body.project_id, body.email, identity.user_id)
    return success_response(data=_to_recipient_out(recipient))


@router.delete(
    "/recipients/{recipient_id}",
    response_model=SuccessResponse[dict],
    summary="Remove an automailer recipient",
    description="""Removes one recipient from a project's (or global) daily report recipient list.

**When to use:** offboarding a stakeholder who should no longer receive the daily report email.

**Errors:** 403 if the caller isn't permitted, isn't assigned to the recipient's project, or (for a global
recipient) isn't an Admin; 404 if `recipient_id` doesn't exist.""",
    responses=success_and_error_responses(
        "Recipient removed", {"deleted": True}, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE, not_found_example("Recipient")
    ),
)
async def delete_recipient(
    recipient_id: str = Path(..., description="The recipient record to remove.", examples=["6aae5e2f12975e28491efb4a"]),
    identity: IdentityContext = Depends(require_permission("report.configure_recipients")),
) -> dict:
    recipient = await report_repo.get_recipient(recipient_id)
    if recipient is None:
        raise NotFoundError("Recipient not found")
    if recipient.project_id is None:
        if not identity.has_all_projects:
            raise ForbiddenError("Only an Admin may remove a global recipient")
    else:
        ensure_project_access(identity, str(recipient.project_id))
    await report_repo.remove_recipient(recipient_id)
    return success_response(data={"deleted": True})


@router.post(
    "/run",
    response_model=SuccessResponse[ReportRunOut],
    summary="Trigger an on-demand report run",
    description="""Runs and sends the report email for a scope right now, outside the daily schedule — the
exact same computation/render/send pipeline the scheduled job uses (see automailer docs at the service
level).

**When to use:** re-sending a day's report on request, or getting today's numbers immediately instead of
waiting for the scheduled send.

**Required inputs:** `scope` (a project id, or `"all"`). At least one recipient must resolve — either
already configured for the scope, or passed via `extra_recipients`.

**Processing behavior:** the reporting window is always "yesterday" in Asia/Kolkata time (start-inclusive,
end-exclusive) — the same window the daily scheduler uses; this endpoint doesn't take a custom date range.
Unlike a scheduled run, a manual run is never blocked by an existing run for the same scope/day — it always
gets a fresh idempotency key, so it's always safe to re-trigger. Requires `report.trigger_manual`;
`scope="all"` additionally requires the Admin role.

**Errors:** 403 if the caller lacks the permission, isn't assigned to `scope`, or (for `scope="all"`) isn't
an Admin; 400 if no recipient resolves for the scope.""",
    responses=success_and_error_responses(
        "Run completed",
        _RUN_EXAMPLE,
        UNAUTHORIZED_EXAMPLE,
        FORBIDDEN_EXAMPLE,
        error_example(
            "No recipients configured", 400, "No recipients configured for this scope; add one or pass extra_recipients", "BAD_REQUEST"
        ),
    ),
)
async def trigger_manual_run(
    request: Request,
    body: ManualRunRequest,
    identity: IdentityContext = Depends(require_permission("report.trigger_manual")),
) -> dict:
    if body.scope == "all":
        if not identity.has_all_projects:
            raise ForbiddenError("Only an Admin may trigger an all-projects report")
    else:
        ensure_project_access(identity, body.scope)

    window_start, window_end = previous_ist_calendar_day()
    configured = await report_repo.list_recipients(None if body.scope == "all" else body.scope)
    recipients = sorted({r.email for r in configured} | set(body.extra_recipients))
    if not recipients:
        raise BadRequestError("No recipients configured for this scope; add one or pass extra_recipients")

    run = await run_report(
        scope=body.scope,
        window_start=window_start,
        window_end=window_end,
        recipients=recipients,
        triggered_by="manual",
        # A single-project scope reads from that project's own configured
        # report db/collection, same as /calls, /dashboard and /export.csv
        # (see core.deps.resolve_collection_for_project); getattr guards
        # against app.state.users_http_client never being set, which is only
        # the case for a test app whose lifespan hasn't run.
        users_http_client=getattr(request.app.state, "users_http_client", None),
    )
    return success_response(data=_to_run_out(run))


@router.get(
    "/runs",
    response_model=SuccessResponse[list[ReportRunOut]],
    summary="List report run history",
    description="""Returns past report runs (scheduled and manual), newest first.

**When to use:** auditing whether/when a report was sent, or diagnosing a failed send.

**Processing behavior:** with no `scope`, a non-Admin caller sees only runs for their own assigned
projects (never a cross-project leak); an Admin sees everything. `scope="all"` (the combined-projects
history) is itself Admin-only. Requires `report.view`.

**Errors:** 403 if `scope` is a project the caller isn't assigned to, or `scope="all"` and the caller isn't
an Admin.""",
    responses=success_and_error_responses("Run history", [_RUN_EXAMPLE], UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE),
)
async def get_runs(
    scope: str | None = Query(
        default=None,
        description='Filter to one project id, or `"all"` for the combined-projects run history (Admin only). Omit to see every run the caller may view.',
        examples=["6aae5deae9a48c4dc62aa398"],
    ),
    status: str | None = Query(
        default=None,
        description='Filter by outcome: `"pending"`, `"running"`, `"success"`, `"failed"`, or `"skipped_duplicate"`.',
        examples=["success"],
    ),
    identity: IdentityContext = Depends(require_permission("report.view")),
) -> dict:
    if scope is not None:
        if scope == "all" and not identity.has_all_projects:
            raise ForbiddenError("Only an Admin may view the all-projects report history")
        if scope != "all":
            ensure_project_access(identity, scope)
        effective_scope: str | list[str] | None = scope
    elif identity.has_all_projects:
        effective_scope = None
    else:
        # No explicit filter from a non-Admin caller: scope down to exactly
        # their own assigned projects rather than exposing every run in the
        # system (list_runs(scope=None) would otherwise be a cross-project
        # data leak for Supervisor/Viewer).
        effective_scope = list(identity.project_ids or [])
    runs = await report_repo.list_runs(scope=effective_scope, status=status)
    return success_response(data=[_to_run_out(r) for r in runs])
