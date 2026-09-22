from datetime import datetime, timezone

from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError

from reports_service.reports.model import ReportRecipient, ReportRun, ReportRunStatus, ReportRunTrigger


async def create_report_run(
    *,
    run_key: str,
    scope: str,
    window_start: datetime,
    window_end: datetime,
    recipients: list[str],
    triggered_by: ReportRunTrigger,
) -> ReportRun | None:
    """Returns None (not an exception) when `run_key` already exists — the
    unique index is the actual duplicate-prevention mechanism; this just
    turns that into a normal "no new run was created" result for the caller.
    """

    run = ReportRun(
        run_key=run_key,
        scope=scope,
        window_start=window_start,
        window_end=window_end,
        recipients=recipients,
        status="pending",
        triggered_by=triggered_by,
    )
    try:
        await run.insert()
    except DuplicateKeyError:
        return None
    return run


async def mark_status(
    run: ReportRun,
    status: ReportRunStatus,
    *,
    attempts: int | None = None,
    errors: list[str] | None = None,
) -> ReportRun:
    run.status = status
    if attempts is not None:
        run.attempts = attempts
    if errors:
        run.errors.extend(errors)
    run.updated_at = datetime.now(timezone.utc)
    await run.save()
    return run


async def list_runs(scope: str | list[str] | None = None, status: ReportRunStatus | None = None) -> list[ReportRun]:
    query: dict = {}
    if scope is not None:
        query["scope"] = {"$in": scope} if isinstance(scope, list) else scope
    if status is not None:
        query["status"] = status
    return await ReportRun.find(query).sort(-ReportRun.created_at).to_list()


async def get_by_run_key(run_key: str) -> ReportRun | None:
    return await ReportRun.find_one(ReportRun.run_key == run_key)


async def list_recipients(project_id: str | None) -> list[ReportRecipient]:
    """project_id=None lists ONLY the global recipients; a project-scoped
    listing (project_id given) returns that project's recipients plus the
    global ones, since a global recipient is meant to receive every report.
    """

    if project_id is None:
        return await ReportRecipient.find(ReportRecipient.project_id == None).to_list()  # noqa: E711
    return await ReportRecipient.find(
        {"$or": [{"project_id": PydanticObjectId(project_id)}, {"project_id": None}]}
    ).to_list()


async def add_recipient(project_id: str | None, email: str, added_by: str) -> ReportRecipient:
    recipient = ReportRecipient(
        project_id=PydanticObjectId(project_id) if project_id is not None else None,
        email=email,
        added_by=PydanticObjectId(added_by),
    )
    await recipient.insert()
    return recipient


async def get_recipient(recipient_id: str) -> ReportRecipient | None:
    return await ReportRecipient.get(PydanticObjectId(recipient_id))


async def remove_recipient(recipient_id: str) -> bool:
    recipient = await get_recipient(recipient_id)
    if recipient is None:
        return False
    await recipient.delete()
    return True
