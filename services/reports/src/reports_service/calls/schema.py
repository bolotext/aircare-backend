from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MappedColumn(BaseModel):
    """One Conversation Logs column configured in the project's Field Mapper."""

    key: str = Field(description="Canonical (preferred) field name; also the key in each item's `fields`.")
    raw_field: str = Field(description="The raw source field it maps to.")
    restricted: bool = Field(description="True when the raw field holds personal data and its values are withheld.")


class CallListItem(BaseModel):
    """One call record as shown in the paginated `/calls` list — a summary
    view (no `raw` transcript payload; fetch `/calls/{conversation_id}` for
    that)."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "conversation_id": "1789455660779658485",
                "project_id": "6aae5deae9a48c4dc62aa398",
                "bot_id": "9a7fc57512664d999af648103b047bda",
                "language": "kn-IN",
                "call_status": "ANSWERED",
                "disposition": "NEUTRAL",
                "call_start_time": "2026-09-15T07:01:01Z",
                "call_connected_time": "2026-09-15T07:01:07Z",
                "call_end_time": "2026-09-15T07:01:36Z",
                "connected_seconds": 29.0,
                "tool_failure_count": 0,
            }
        }
    )

    conversation_id: str = Field(description="Stable, unique identifier for the call — the primary key clients use to fetch call detail.")
    project_id: str = Field(description="The project (Mongo ObjectId) this call belongs to.")
    bot_id: str = Field(description="Identifier of the voice bot/flow that handled this call.")
    language: str = Field(description="BCP-47-style language/locale code the call was conducted in (e.g. `en-IN`, `hi-IN`, `kn-IN`).")
    call_status: str = Field(description='Telephony call outcome, e.g. `"ANSWERED"`, `"NO_ANSWER"`, `"COMPLETED"`.')
    disposition: str = Field(description="Business outcome assigned to the call, e.g. `RESOLVED`, `ABANDONED`, `TRANSFERRED_SUCCESSFULLY`.")
    call_start_time: datetime = Field(description="UTC timestamp the call was initiated.")
    call_connected_time: datetime | None = Field(description="UTC timestamp the call was answered/connected, or `null` if it never connected.")
    call_end_time: datetime | None = Field(description="UTC timestamp the call ended, or `null` if it's still open/was never connected.")
    connected_seconds: float | None = Field(description="Talk time in seconds while connected, or `null` when not applicable (e.g. unanswered).")
    tool_failure_count: int = Field(description="Number of tool/function-call invocations during this call that failed.")
    fields: dict[str, Any] = Field(default_factory=dict, description="Values of the project's Field Mapper columns; populated on `GET /calls` only.")


class CallDetail(CallListItem):
    """Full call record, including the raw per-call operational payload
    (`raw`) used to render transcript/event-timeline/tool-result views.
    `raw`'s exact keys vary by project/data source — treat it as
    opaque, project-specific detail rather than a fixed schema."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                **CallListItem.model_config["json_schema_extra"]["example"],
                "organization_id": "6aae5deae9a48c4dc62aa397",
                "raw": {"conversation_log": [{"role": "assistant", "content": "Hello, how can I help you today?"}]},
                "recording_available": True,
            }
        }
    )

    organization_id: str = Field(description="The organization (Mongo ObjectId) that owns this call's project.")
    raw: dict = Field(description="Project-specific operational detail not modeled as typed fields above (e.g. conversation transcript, event timeline). PII fields, if any, have already been stripped by the source's schema adapter before this response is built.")
    source_document: dict = Field(
        default_factory=dict,
        description="The call's full source Mongo document (all top-level fields). Personal-data fields keep their key but their value is withheld.",
    )
    recording_available: bool = Field(
        description="Whether `GET /calls/{conversation_id}/recording` has a recording to serve for this call — "
        "lets a caller decide to fetch it right away instead of waiting for a separate user action. Always "
        "`false` for a project whose configured source has no known recording (see that route's docs)."
    )


class PaginatedCalls(BaseModel):
    """A single page of `/calls` results plus the pagination metadata needed to fetch the next one."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "items": [CallListItem.model_config["json_schema_extra"]["example"]],
                "page": 1,
                "page_size": 20,
                "total": 137,
            }
        }
    )

    columns: list[MappedColumn] = Field(default_factory=list, description="The project's Field Mapper columns, in order. Empty when none are configured.")
    items: list[CallListItem] = Field(description="Calls on this page, most recent (`call_start_time`) first.")
    page: int = Field(description="The page number this response corresponds to (1-indexed).")
    page_size: int = Field(description="Number of items per page that was actually applied (see the `page_size` query parameter's cap).")
    total: int = Field(description="Total number of calls matching the applied filters, across all pages.")
