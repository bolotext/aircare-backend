from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RecipientCreate(BaseModel):
    """Adds an email address to a scope's daily automailer recipient list."""

    model_config = ConfigDict(
        json_schema_extra={"example": {"project_id": "6aae5deae9a48c4dc62aa398", "email": "qa-test@example.com"}}
    )

    project_id: str | None = Field(
        default=None,
        description="Project (Mongo ObjectId) this recipient should get reports for. Omit (`null`) to add a "
        "global recipient who gets every project's report — Admin only.",
    )
    email: str = Field(description="Email address to add to the recipient list.", examples=["qa-test@example.com"])


class RecipientOut(BaseModel):
    """A configured automailer recipient."""

    id: str = Field(description="Recipient record id.")
    project_id: str | None = Field(description="Project this recipient is scoped to, or `null` for a global recipient.")
    email: str = Field(description="The recipient's email address.")


class ManualRunRequest(BaseModel):
    """Triggers an on-demand report run outside the daily schedule."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"scope": "6aae5deae9a48c4dc62aa398", "extra_recipients": ["qa-test@example.com"]}
        }
    )

    scope: str = Field(
        description='Either a project id (Mongo ObjectId) to run a single-project report, or the literal '
        '`"all"` for a combined report across every project (Admin only).',
        examples=["6aae5deae9a48c4dc62aa398", "all"],
    )
    extra_recipients: list[str] = Field(
        default_factory=list,
        description="Additional email addresses to send this run to, on top of whatever is already configured for the scope.",
    )


class ReportRunOut(BaseModel):
    """Record of one report run (scheduled or manual) — its window, recipients, and send outcome."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "6aae5e5012975e28491efb4b",
                "run_key": "manual:6aae5deae9a48c4dc62aa398:2026-09-18:6c3bc39b-c436-4a7f-9c1b-8c55b5f4b47b",
                "scope": "6aae5deae9a48c4dc62aa398",
                "window_start": "2026-09-17T18:30:00",
                "window_end": "2026-09-18T18:30:00",
                "recipients": ["qa-test@example.com"],
                "status": "success",
                "attempts": 1,
                "errors": [],
                "triggered_by": "manual",
            }
        }
    )

    id: str | None = Field(description="Report run record id, or `null` for a run skipped as a duplicate before it was persisted.")
    run_key: str = Field(description='Idempotency key for this run — `"scheduled:<scope>:<date>"` for scheduled runs (unique per scope/day), `"manual:<scope>:<date>:<uuid>"` for manual ones (always unique, so a day can always be re-sent on request).')
    scope: str = Field(description='The project id this run covers, or `"all"` for a combined run.')
    window_start: datetime = Field(description="Start (inclusive) of the reporting window this run covers.")
    window_end: datetime = Field(description="End (exclusive) of the reporting window this run covers.")
    recipients: list[str] = Field(description="Email addresses this run was (or will be) sent to.")
    status: str = Field(
        description='Send outcome: `"pending"`, `"running"`, `"success"`, `"failed"`, or `"skipped_duplicate"` '
        "(a scheduled run for this scope+day already existed)."
    )
    attempts: int = Field(description="Number of SMTP send attempts made.")
    errors: list[str] = Field(description="Error messages recorded across any failed send attempts; empty on success.")
    triggered_by: str = Field(description='`"scheduled"` (daily automailer) or `"manual"` (`POST /run`).')
