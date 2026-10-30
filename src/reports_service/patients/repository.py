from dataclasses import dataclass
from datetime import datetime, timezone

from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError

from reports_service.patients.model import Patient

# Retry budget for patient_id generation (see _generate_patient_id) — only
# ever exhausted under concurrent creates racing for the same next number,
# and even then a handful of retries is enough at this collection's scale.
_MAX_ID_GENERATION_ATTEMPTS = 5


@dataclass(frozen=True)
class PatientFilter:
    project_id: str
    condition: str | None = None
    risk_flag: bool | None = None
    do_not_call: bool | None = None


def _build_query(patient_filter: PatientFilter) -> dict:
    query: dict = {"project_id": PydanticObjectId(patient_filter.project_id)}
    if patient_filter.condition:
        query["condition"] = patient_filter.condition
    if patient_filter.risk_flag is not None:
        query["risk_flag"] = patient_filter.risk_flag
    if patient_filter.do_not_call is not None:
        query["do_not_call"] = patient_filter.do_not_call
    return query


async def _generate_patient_id() -> str:
    """`PT-0001`-style sequential id derived from the current document count.
    Not a distributed-safe counter — fine at this collection's scale (a
    single-project-instance demo, not a high-concurrency intake system) —
    but insert() below still retries on a collision instead of trusting it
    blindly, since the unique index is the actual source of truth.
    """

    count = await Patient.find().count()
    return f"PT-{count + 1:04d}"


async def list_paginated(patient_filter: PatientFilter, page: int, page_size: int) -> tuple[list[Patient], int]:
    query = _build_query(patient_filter)
    total = await Patient.find(query).count()
    cursor = Patient.find(query).sort("-created_at").skip((page - 1) * page_size).limit(page_size)
    items = await cursor.to_list()
    return items, total


async def get_by_patient_id(patient_id: str, project_id: str) -> Patient | None:
    return await Patient.find_one(Patient.patient_id == patient_id, Patient.project_id == PydanticObjectId(project_id))


async def get_by_patient_id_any_project(patient_id: str) -> Patient | None:
    """Looks up a patient without already knowing their project — used by
    patient_calls/repository.py to resolve which project a call's
    `patient_id` belongs to (and hence whether the caller may access it),
    since a patient-call record carries no `project_id` of its own.
    """

    return await Patient.find_one(Patient.patient_id == patient_id)


async def list_patient_ids_for_project(project_id: str) -> list[str]:
    """Unpaginated list of every patient_id in a project's roster — used by
    patient_calls/repository.py to scope a project-wide call listing (that
    collection carries no project_id of its own; the patient it belongs to
    is the only link to a project). Fetches full documents rather than a
    Beanie field projection — fine at this roster's scale, and simpler than
    getting projection mechanics exactly right for one field.
    """

    patients = await Patient.find(Patient.project_id == PydanticObjectId(project_id)).to_list()
    return [p.patient_id for p in patients]


async def create_patient(*, project_id: str, is_seeded: bool = False, **fields) -> Patient:
    last_error: DuplicateKeyError | None = None
    for _ in range(_MAX_ID_GENERATION_ATTEMPTS):
        patient = Patient(
            patient_id=await _generate_patient_id(),
            project_id=PydanticObjectId(project_id),
            is_seeded=is_seeded,
            **fields,
        )
        try:
            await patient.insert()
            return patient
        except DuplicateKeyError as exc:
            last_error = exc
    raise last_error  # pragma: no cover - only reachable under sustained concurrent creates


async def update_patient(patient: Patient, **fields) -> Patient:
    for key, value in fields.items():
        if value is not None:
            setattr(patient, key, value)
    patient.updated_at = datetime.now(timezone.utc)
    await patient.save()
    return patient


async def delete_patient(patient: Patient) -> None:
    await patient.delete()
