from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

_LIVE_READING_EXAMPLE = {
    "value": 57.31,
    "timestamp": "2026-09-21T15:30:00Z",
    "raw_response": {
        "pm25_concentration_ugm3": 15.06,
        "location_id": 5548,
        "location_name": "BTM Layout, Bengaluru - CPCB",
        "sensor_id": 12235361,
    },
}

_EXAMPLE = {
    "id": "6ab61209c2da0ae446c3aa9b",
    "call_id": "4ba506a9-8af0-4415-8d82-9fde7e7215f1",
    "patient_id": "PT-0003",
    "patient_name": "Narendra Singh Dangi",
    "phone_number": "+917389058485",
    "city": "Bengaluru",
    "condition": "copd",
    "direction": "outbound",
    "live_aqi_reading": _LIVE_READING_EXAMPLE,
    "second_aqi_reading": None,
    "risk_threshold": 120,
    "aqi_reconciliation_delta": None,
    "reading_timestamp": "2026-09-21T15:30:00Z",
    "status": "APPOINTMENT_INTERESTED",
    "disposition": "APPOINTMENT_INTERESTED",
    "telephony_triggered": False,
    "created_at": "2026-09-21T15:31:00Z",
    "updated_at": "2026-09-21T15:31:00Z",
}


class AqiReadingIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": _LIVE_READING_EXAMPLE})

    value: float = Field(description="AQI (or pollutant concentration) value as reported by the provider.")
    timestamp: str = Field(description="The provider's own timestamp string, stored verbatim (formats vary between providers).")
    raw_response: dict = Field(default_factory=dict, description="Opaque passthrough of the provider's full response payload.")


class PatientCallCreate(BaseModel):
    """Records a new AQI-triggered advisory call. `call_id` is the external
    telephony/AQI system's own call identifier — required, and must be
    unique (this is that system's idempotency key, not ours).
    """

    model_config = ConfigDict(
        json_schema_extra={"example": {k: v for k, v in _EXAMPLE.items() if k not in ("id", "patient_name", "created_at", "updated_at")}}
    )

    call_id: str = Field(min_length=1, max_length=100, description="Externally-assigned unique call identifier.")
    patient_id: str = Field(description="The patient (business id, e.g. `PT-0003`) this call was placed for.", examples=["PT-0003"])
    project_id: str = Field(
        description="Project (Mongo ObjectId) `patient_id` belongs to — required to authorize the call, never stored on the record itself.",
        examples=["6ab4ceafe9f5e5190f56b7e9"],
    )
    phone_number: str = Field(min_length=1, max_length=32)
    city: str = Field(min_length=1, max_length=100)
    condition: str = Field(min_length=1, max_length=100)
    direction: str = Field(min_length=1, max_length=20, description="e.g. `outbound`, `inbound`.")
    live_aqi_reading: AqiReadingIn
    second_aqi_reading: AqiReadingIn | None = None
    risk_threshold: int = Field(description="The condition's configured risk_threshold_aqi at the time this call was triggered.")
    aqi_reconciliation_delta: float | None = None
    reading_timestamp: datetime
    status: str = Field(min_length=1, max_length=100)
    disposition: str | None = None
    telephony_triggered: bool = False


class PatientCallUpdate(BaseModel):
    """Partial update — only the fields supplied are changed. Typically used
    to attach `second_aqi_reading`/`aqi_reconciliation_delta` once
    reconciliation runs, or to update `status`/`disposition`/
    `telephony_triggered` as the call progresses.
    """

    model_config = ConfigDict(json_schema_extra={"example": {"status": "APPOINTMENT_BOOKED", "telephony_triggered": True}})

    phone_number: str | None = Field(default=None, min_length=1, max_length=32)
    city: str | None = Field(default=None, min_length=1, max_length=100)
    condition: str | None = Field(default=None, min_length=1, max_length=100)
    direction: str | None = Field(default=None, min_length=1, max_length=20)
    second_aqi_reading: AqiReadingIn | None = None
    aqi_reconciliation_delta: float | None = None
    status: str | None = Field(default=None, min_length=1, max_length=100)
    disposition: str | None = None
    telephony_triggered: bool | None = None


class PatientCallOut(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": _EXAMPLE})

    id: str = Field(description="Mongo document id — distinct from `call_id`, the external system's own identifier.")
    call_id: str
    patient_id: str
    patient_name: str | None = None
    phone_number: str
    city: str
    condition: str
    direction: str
    live_aqi_reading: AqiReadingIn
    second_aqi_reading: AqiReadingIn | None
    risk_threshold: int
    aqi_reconciliation_delta: float | None
    reading_timestamp: datetime
    status: str
    disposition: str | None
    telephony_triggered: bool
    created_at: datetime
    updated_at: datetime


class PaginatedPatientCalls(BaseModel):
    items: list[PatientCallOut]
    page: int
    page_size: int
    total: int
