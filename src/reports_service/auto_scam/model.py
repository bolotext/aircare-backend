"""Auto Scam: upload conversation JSON, run scam.py (lead-score analytics) on it, review the output.

Collections
- `auto_scam`          one document per uploaded conversation (the raw export, top-level fields kept as-is).
                       `project_id` holds the uploaded FILE NAME; `aircare_project_id` ties it to the
                       Aircare project for access control. Accessed through the raw collection (no Beanie
                       model) because the conversation shape is free-form.
- `auto_scam_results`  one row per conversation, keyed by customerCRTId: previous vs updated value of every analytics output field.
                       A run skips conversations that already have a successful row here.
- `auto_scam_datasets` one per uploaded file: its analytics settings and last run.
- `auto_scam_runs`     run status, progress and the log tail.
"""

from datetime import datetime, timezone

from beanie import Document, PydanticObjectId
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import IndexModel

from reports_service.auto_scam.rules import MAX_RULES, Rule, default_rules

RECORDS_COLLECTION = "auto_scam"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UpdatedColumn(BaseModel):
    """A field whose value after the rules ran is shown as its own column, under a name the user chooses."""

    field: str = Field(min_length=1, max_length=100, description="e.g. STAGE_CODE, DISPOSITION, INTEREST_CLASSIFICATION")
    name: str = Field(min_length=1, max_length=60, description="Column title shown to users, e.g. 'New disposition'.")

    @field_validator("field", "name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


def default_updated_columns() -> list[UpdatedColumn]:
    return [UpdatedColumn(field="INTEREST_CLASSIFICATION", name="Updated class")]


class AutoScamConfig(BaseModel):
    prompt: str = Field(min_length=1, description="The analytics prompt, saved on the dataset itself. Used as plain text, exactly as scam.py always has (the bundled analytics_prompt.yaml is not strict YAML).")
    rules: list[Rule] = Field(
        default_factory=default_rules, max_length=MAX_RULES,
        description="Post-processing rules applied to the model's output (conditions on STAGE_CODE, setupTime, ...).",
    )
    updated_columns: list[UpdatedColumn] = Field(
        default_factory=default_updated_columns, max_length=10,
        description="Which updated fields to show in the Conversations/Results tables, and what to call them.",
    )
    workers: int = Field(5, ge=1, le=50)
    max_retries: int = Field(3, ge=0, le=10)

    @model_validator(mode="after")
    def _unique_columns(self):
        fields = [c.field for c in self.updated_columns]
        if len(fields) != len(set(fields)):
            raise ValueError("each updated column must use a different field")
        return self


class AutoScamDataset(Document):
    aircare_project_id: PydanticObjectId
    file_name: str  # == `project_id` on the conversation documents
    config: AutoScamConfig
    created_by: str
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "auto_scam_datasets"
        indexes = [IndexModel([("aircare_project_id", 1), ("file_name", 1)], unique=True)]


class AutoScamRun(Document):
    aircare_project_id: PydanticObjectId
    file_name: str
    status: str = "queued"  # queued | running | succeeded | completed_with_errors | failed | cancelled
    config: AutoScamConfig
    # Snapshot of the dataset's prompt taken when the run is queued, so history stays reproducible after edits.
    prompt_text: str = ""
    total: int | None = None
    processed: int = 0
    failed: int = 0
    log: str = ""  # tail of scam.py output, kept in the DB (no files on disk)
    exit_code: int | None = None
    error: str | None = None
    # Subset to analyse: explicit conversation ids, or a search (every match); both empty = the whole file.
    record_ids: list[str] = Field(default_factory=list)
    search: str | None = None
    force: bool = False  # re-analyse conversations that already have a result
    skipped: int = 0  # conversations skipped because they were already analysed
    cancel_requested: bool = False
    heartbeat_at: datetime | None = None
    started_by: str
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    class Settings:
        name = "auto_scam_runs"
        indexes = [IndexModel([("aircare_project_id", 1), ("file_name", 1), ("created_at", -1)])]


class AutoScamResult(Document):
    customer_crt_id: str  # the identity of the conversation (see records.record_key)
    run_id: str  # the run that produced this result
    aircare_project_id: PydanticObjectId
    file_name: str
    record_id: str
    disposition: str | None = None
    previous_stage_code: str | None = None
    updated_stage_code: str | None = None  # scam.py never rewrites STAGE_CODE, so this equals the previous one
    # One entry per analytics output field, under its original name: {field, previous, updated, changed}.
    # `previous` is the value the uploaded conversation already had for that field. Empty = analysis failed.
    fields: list[dict] = Field(default_factory=list)
    changed: bool = False  # any field changed
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "auto_scam_results"
        indexes = [
            IndexModel([("aircare_project_id", 1), ("file_name", 1), ("customer_crt_id", 1)], unique=True),
            IndexModel([("customer_crt_id", 1)]),
        ]
