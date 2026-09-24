from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

_EXAMPLE = {
    "patient_id": "PT-0001",
    "project_id": "6aae5deae9a48c4dc62aa398",
    "full_name": "Synthetic Test Name 01",
    "phone_number": "+910000000001",
    "dob": "1985-04-12",
    "city": "Delhi",
    "condition": "asthma",
    "preferred_language": "hindi",
    "risk_flag": True,
    "do_not_call": False,
    "last_advisory_episode": None,
    "is_seeded": True,
    "created_at": "2026-09-01T10:00:00Z",
    "updated_at": "2026-09-01T10:00:00Z",
}


class PatientCreate(BaseModel):
    """Adds a patient to a project's roster. `patient_id` is always
    server-generated (see patients.repository) — never accepted from the
    caller — so it can't collide or be spoofed.
    """

    model_config = ConfigDict(json_schema_extra={"example": {k: v for k, v in _EXAMPLE.items() if k != "patient_id"}})

    project_id: str = Field(description="Project (Mongo ObjectId) this patient belongs to.", examples=["6aae5deae9a48c4dc62aa398"])
    full_name: str = Field(min_length=1, max_length=200)
    phone_number: str = Field(min_length=1, max_length=32)
    dob: date
    city: str = Field(min_length=1, max_length=100)
    condition: str = Field(min_length=1, max_length=100, description="Respiratory condition, e.g. `asthma`, `copd`.")
    preferred_language: str = Field(min_length=1, max_length=50)
    risk_flag: bool = Field(default=False, description="Marks this patient for closer advisory follow-up.")
    do_not_call: bool = Field(default=False, description="Excludes this patient from outbound advisory calls.")


class PatientUpdate(BaseModel):
    """Partial update — every field is optional; only the ones supplied are changed."""

    model_config = ConfigDict(json_schema_extra={"example": {"risk_flag": True, "do_not_call": False}})

    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    phone_number: str | None = Field(default=None, min_length=1, max_length=32)
    dob: date | None = None
    city: str | None = Field(default=None, min_length=1, max_length=100)
    condition: str | None = Field(default=None, min_length=1, max_length=100)
    preferred_language: str | None = Field(default=None, min_length=1, max_length=50)
    risk_flag: bool | None = None
    do_not_call: bool | None = None
    last_advisory_episode: datetime | None = None


class PatientOut(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": _EXAMPLE})

    patient_id: str
    project_id: str
    full_name: str
    phone_number: str
    dob: date
    city: str
    condition: str
    preferred_language: str
    risk_flag: bool
    do_not_call: bool
    last_advisory_episode: datetime | None
    is_seeded: bool
    created_at: datetime
    updated_at: datetime


class PaginatedPatients(BaseModel):
    items: list[PatientOut]
    page: int
    page_size: int
    total: int
