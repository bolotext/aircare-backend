"""AQI-triggered patient advisory calls: outbound calls placed to a patient
(see patients/model.py) when their condition's risk threshold (see
risk_thresholds/model.py) is exceeded, with the AQI readings that triggered
and later reconciled the call.

Deliberately shares its physical collection with `Call` (see
calls/model.py::Settings) — this shape is written there today by an external
AQI/telephony system, and `call_id` (external, required, unique) is that
system's own idempotency key, mirroring how `Call.conversation_id` is treated
as an externally-supplied key rather than server-generated. The two shapes
never collide in a query: every query here filters on `call_id`/`patient_id`
(absent from voice-call CDR documents), and every voice-call query filters on
`project_id`/`conversation_id` (absent here) — see
calls/schema_adapters.py's `_PATIENT_ADVISORY_CALL_EXCLUSION` for the one
place that needed to know about this explicitly (schema-shape sampling).

Carries no `project_id` of its own — a record's project is whatever project
its `patient_id` belongs to (see patients/model.py), resolved via a lookup in
patient_calls/repository.py, never stored redundantly here.
"""

from datetime import datetime, timezone

from beanie import Document
from pydantic import BaseModel, Field
from pymongo import IndexModel


class AqiReading(BaseModel):
    """One AQI reading as returned by whichever upstream AQI provider served
    it. `timestamp` is kept as the provider's own raw string (observed
    formats vary: `"...Z"` vs `"...+05:30"`) rather than parsed into a
    `datetime`, and `raw_response` is an opaque passthrough of that
    provider's full payload — same "never reshape a third-party payload"
    principle as `Call.raw`.
    """

    value: float
    timestamp: str
    raw_response: dict = Field(default_factory=dict)


class PatientCall(Document):
    call_id: str
    patient_id: str
    # Denormalized from Patient.full_name at create time (see
    # router.create_patient_call), same rationale as phone_number/city/
    # condition below — avoids a join on every list/detail read. Optional
    # since older records (and any written directly by the external
    # AQI/telephony system) may predate this field.
    patient_name: str | None = None

    phone_number: str
    city: str
    condition: str
    direction: str

    live_aqi_reading: AqiReading
    second_aqi_reading: AqiReading | None = None
    risk_threshold: int
    aqi_reconciliation_delta: float | None = None
    reading_timestamp: datetime

    status: str
    disposition: str | None = None
    telephony_triggered: bool = False

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "calls"
        indexes = [
            # sparse=True is load-bearing, not cosmetic: this collection also
            # holds native voice-call `Call` documents (see calls/model.py)
            # which have no `call_id` field at all. A non-sparse unique
            # index treats every one of those as `call_id: null` and MongoDB
            # only permits *one* null in a non-sparse unique index — the
            # second voice-call insert would then fail with a duplicate-key
            # error unrelated to any real call_id collision.
            IndexModel("call_id", unique=True, sparse=True),
            IndexModel([("patient_id", 1), ("created_at", -1)], sparse=True),
        ]
