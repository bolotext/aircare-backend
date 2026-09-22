"""Persisted automailer run history (`ReportRun`) and permitted-user-configured
extra recipients (`ReportRecipient`). A scheduled run's `run_key` is the
idempotency key that stops routine duplicate sends (see mailer/pipeline.py);
a manual run's `run_key` always includes a fresh uuid so it never collides
with — or gets blocked by — that day's scheduled run.
"""

from datetime import datetime, timezone
from typing import Literal

from beanie import Document, PydanticObjectId
from pydantic import Field
from pymongo import IndexModel

ReportRunStatus = Literal["pending", "running", "success", "failed", "ambiguous", "skipped_duplicate"]
ReportRunTrigger = Literal["scheduled", "manual"]


class ReportRun(Document):
    run_key: str
    scope: str  # a project_id, or the literal "all"
    window_start: datetime
    window_end: datetime
    recipients: list[str]
    status: ReportRunStatus
    attempts: int = 0
    errors: list[str] = Field(default_factory=list)
    triggered_by: ReportRunTrigger
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "report_runs"
        indexes = [
            IndexModel("run_key", unique=True),
            IndexModel([("scope", 1), ("window_start", -1)]),
        ]


class ReportRecipient(Document):
    # None = a global/all-projects recipient (ADMIN-only to configure).
    project_id: PydanticObjectId | None = None
    email: str
    added_by: PydanticObjectId
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "report_recipients"
        indexes = [IndexModel([("project_id", 1)])]
