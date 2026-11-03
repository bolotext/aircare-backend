"""Auto Scam: upload conversation JSON, run scam.py (lead-score analytics) on it, review the output.

Collections
- `auto_scam`          one document per uploaded conversation (the raw export, top-level fields kept as-is).
                       `project_id` holds the uploaded FILE NAME; `aircare_project_id` ties it to the
                       Aircare project for access control. Accessed through the raw collection (no Beanie
                       model) because the conversation shape is free-form.
- `auto_scam_results`  one row per conversation, keyed by customerCRTId: the analytics JSON the model produced (after rules).
                       A run skips conversations that already have a successful row here.
- `auto_scam_datasets` one per uploaded file: its analytics settings and last run.
- `auto_scam_runs`     run status, progress and the log tail.
- `auto_scam_uploads`  chunked big-file uploads in flight (progress, status); the data itself lands in `auto_scam`.
"""

from datetime import datetime, timezone

from beanie import Document, PydanticObjectId
from pydantic import BaseModel, Field, computed_field, field_validator, model_validator
from pymongo import IndexModel

from reports_service.auto_scam.rules import MAX_RULES, Rule, default_rules

RECORDS_COLLECTION = "auto_scam"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FieldMapping(BaseModel):
    """One row of the field mapper (see mapper.py): analytics JSON key  ->  conversation field."""

    source: str = Field(min_length=1, max_length=200, description="Key or dotted path in the analytics JSON, e.g. 'INTEREST_CLASSIFICATION' or 'result.disposition'.")
    field: str = Field(min_length=1, max_length=100, description="The conversation field it updates, e.g. STAGE_CODE, DISPOSITION, INTEREST_CLASSIFICATION.")

    @field_validator("source", "field")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @computed_field  # type: ignore[prop-decorator]
    @property
    def name(self) -> str:
        """Title of the 'updated' column: derived, so the user only maps key -> field."""
        return f"New {self.field}"


def default_field_mapper() -> list[FieldMapping]:
    return [FieldMapping(source="INTEREST_CLASSIFICATION", field="INTEREST_CLASSIFICATION")]


class AutoScamConfig(BaseModel):
    prompt: str = Field(min_length=1, description="The analytics prompt, saved on the dataset itself. Used as plain text, exactly as scam.py always has (the bundled analytics_prompt.yaml is not strict YAML).")
    rules: list[Rule] = Field(
        default_factory=default_rules, max_length=MAX_RULES,
        description="Post-processing rules applied to the model's output (conditions on STAGE_CODE, setupTime, ...).",
    )
    field_mapper: list[FieldMapping] = Field(
        default_factory=default_field_mapper, max_length=20,
        description="Maps keys of the analytics JSON to conversation fields; each becomes an 'updated' column.",
    )
    workers: int = Field(5, ge=1, le=50)
    max_retries: int = Field(3, ge=0, le=10)

    @model_validator(mode="before")
    @classmethod
    def _migrate_updated_columns(cls, data):
        """Datasets saved before the field mapper stored `updated_columns` [{field, name}] (analytics key == field)."""
        if isinstance(data, dict) and "field_mapper" not in data and data.get("updated_columns"):
            data = {**data, "field_mapper": [{"source": c["field"], "field": c["field"]} for c in data["updated_columns"]]}
        return data

    @model_validator(mode="after")
    def _unique_fields(self):
        fields = [m.field for m in self.field_mapper]
        if len(fields) != len(set(fields)):
            raise ValueError("each conversation field can be mapped only once")
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
    filters: dict | None = None  # the filters whose every match was selected ("select all N"); None = not a filter run
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
    # Every top-level key of the analytics JSON the run produced: {field, updated}. The mapper turns these into
    # previous/updated columns at read time (see mapper.py). Empty = the analysis failed.
    fields: list[dict] = Field(default_factory=list)
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "auto_scam_results"
        indexes = [
            IndexModel([("aircare_project_id", 1), ("file_name", 1), ("customer_crt_id", 1)], unique=True),
            IndexModel([("customer_crt_id", 1)]),
        ]


class AutoScamUpload(Document):
    """A large file being uploaded in chunks. Chunks are appended to a temp file; once complete it is parsed in
    blocks (bounded memory) into `auto_scam`, with progress reported here."""

    aircare_project_id: PydanticObjectId
    file_name: str
    overwrite: bool = False
    size: int
    received: int = 0
    status: str = "receiving"  # receiving | processing | done | failed | cancelled
    processed_bytes: int = 0
    records: int = 0
    error: str | None = None
    path: str
    created_by: str
    created_at: datetime = Field(default_factory=utcnow)
    heartbeat_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None

    class Settings:
        name = "auto_scam_uploads"
        indexes = [IndexModel([("aircare_project_id", 1), ("created_at", -1)])]
