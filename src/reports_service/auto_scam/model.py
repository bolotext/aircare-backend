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
from typing import Literal

from beanie import Document, PydanticObjectId
from pydantic import BaseModel, Field, computed_field, field_validator, model_validator
from pymongo import IndexModel

from reports_service.auto_scam.mapper import RULE_KEYS
from reports_service.auto_scam.rules import MAX_RULES, Rule, default_rules

RECORDS_COLLECTION = "auto_scam"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FieldMapping(BaseModel):
    """One column to show (see mapper.py)."""

    label: str = Field(min_length=1, max_length=60, description="Column title, e.g. 'Customer name' or 'Reason'.")
    origin: Literal["analytics", "model", "rules", "conversation"] = Field(
        "analytics",
        description="Where the value comes from: the final analytics JSON; what the model said before the rules (model); "
        "what the rules changed (rules, with key = changes / rules / conditions / changed / details); or the uploaded conversation.",
    )
    key: str = Field(min_length=1, max_length=200, description="Key or dotted path, e.g. 'REASON', 'result.reason' or 'user_context.name'.")

    @field_validator("label", "key")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @model_validator(mode="after")
    def _rules_key(self):
        if self.origin == "rules" and self.key not in RULE_KEYS:
            raise ValueError(f"for rule changes the key must be one of: {', '.join(RULE_KEYS)}")
        return self


def default_field_mapper() -> list[FieldMapping]:
    return [FieldMapping(label="Lead score", origin="analytics", key="INTEREST_CLASSIFICATION")]


def _upgrade_mapping(item):
    """Earlier shape: {source, field[, name]} = analytics key -> conversation field. Keep it as one analytics column."""
    if isinstance(item, dict) and "key" not in item and "source" in item:
        return {"label": item.get("name") or f"New {item.get('field', item['source'])}", "origin": "analytics", "key": item["source"]}
    return item


class AutoScamConfig(BaseModel):
    prompt: str = Field(min_length=1, description="The analytics prompt, saved on the dataset itself. Used as plain text, exactly as scam.py always has (the bundled analytics_prompt.yaml is not strict YAML).")
    rules: list[Rule] = Field(
        default_factory=default_rules, max_length=MAX_RULES,
        description="Post-processing rules applied to the model's output (conditions on STAGE_CODE, setupTime, ...).",
    )
    field_mapper: list[FieldMapping] = Field(
        default_factory=default_field_mapper, max_length=20,
        description="Extra columns to show: each is a key of the analytics JSON or a field of the conversation.",
    )
    workers: int = Field(5, ge=1, le=50)
    max_retries: int = Field(3, ge=0, le=10)

    @model_validator(mode="before")
    @classmethod
    def _migrate_older_settings(cls, data):
        """Datasets saved with `updated_columns` or the earlier {source, field} mapper keep working."""
        if not isinstance(data, dict):
            return data
        if isinstance(data.get("field_mapper"), list):
            return {**data, "field_mapper": [_upgrade_mapping(i) for i in data["field_mapper"]]}
        if "field_mapper" not in data and data.get("updated_columns"):
            return {**data, "field_mapper": [{"label": c["name"], "origin": "analytics", "key": c["field"]} for c in data["updated_columns"]]}
        return data

    @model_validator(mode="after")
    def _unique_labels(self):
        labels = [m.label for m in self.field_mapper]
        if len(labels) != len(set(labels)):
            raise ValueError("each column needs a different title")
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
    # What the model said BEFORE the rules ran ({field, value} per key; empty = not recorded, e.g. an older run), and what
    # the rules changed: [{index, rule, match, conditions: [{field, op, value, actual, met}], changes: [{field, from, to}]}].
    llm_fields: list[dict] = Field(default_factory=list)
    rule_trace: list[dict] = Field(default_factory=list)
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
