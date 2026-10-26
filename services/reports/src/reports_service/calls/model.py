"""The `Call` document is deliberately a strict, indexed subset of fields
used for filtering/pagination/aggregation, plus a `raw` passthrough for the
deeply-nested real call-record shape (conversation_log, call_infra,
disposition_result, ...) used only for call-detail rendering (transcript,
event timeline, tool results). Modeling the full nested shape as typed Beanie
fields would churn every time the upstream voice-platform schema changes;
the typed fields here are exactly the ones the reporting/RBAC/filtering
logic needs to reason about.
"""

from datetime import datetime, timezone

from beanie import Document, PydanticObjectId
from pydantic import Field, PrivateAttr
from pymongo import IndexModel


class Call(Document):
    project_id: PydanticObjectId
    organization_id: PydanticObjectId

    conversation_id: str
    bot_id: str
    language: str
    call_status: str
    disposition: str

    call_start_time: datetime
    call_connected_time: datetime | None = None
    call_end_time: datetime | None = None
    connected_seconds: float | None = None

    tool_failure_count: int = 0

    # Passthrough for the real conversation_log/call_infra/disposition_result/
    # etc. shape — used for call-detail rendering, never for filtering.
    raw: dict = Field(default_factory=dict)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # The source Mongo document this Call was read from, set by the read paths
    # in calls.repository. Never serialized; only used to resolve a project's
    # Field Mapper columns (see calls.mapped_fields).
    _source_doc: dict | None = PrivateAttr(default=None)

    class Settings:
        name = "calls"
        indexes = [
            IndexModel([("project_id", 1), ("call_start_time", -1)]),
            IndexModel([("project_id", 1), ("disposition", 1)]),
            IndexModel([("project_id", 1), ("language", 1)]),
            # sparse=True is load-bearing, not cosmetic: this collection also
            # holds patient-advisory-call documents (see
            # patient_calls/model.py) which have no `conversation_id` field
            # at all. A non-sparse unique index treats every one of those as
            # `conversation_id: null`, and MongoDB only permits *one* null in
            # a non-sparse unique index — the second patient-advisory-call
            # insert would then fail with a duplicate-key error unrelated to
            # any real conversation_id collision.
            IndexModel("conversation_id", unique=True, sparse=True),
            IndexModel("organization_id"),
        ]
