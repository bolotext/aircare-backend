"""CRUD for AQI-triggered patient advisory calls. This resource carries no
`project_id` of its own (see model.py) — every route resolves project access
through the call's `patient_id` (via patients.repository), so a caller only
ever sees/touches calls for patients in a project they're assigned to,
exactly like `/patients` and `/calls`.
"""

from common.errors import ConflictError, NotFoundError
from common.openapi import FORBIDDEN_EXAMPLE, UNAUTHORIZED_EXAMPLE, not_found_example, success_and_error_responses
from common.responses import success_response
from common.schemas import SuccessResponse
from fastapi import APIRouter, Depends, Path, Query

from reports_service.core.config import get_settings
from reports_service.core.deps import ensure_project_access, require_permission
from reports_service.core.users_client import IdentityContext
from reports_service.patient_calls import repository as call_repo
from reports_service.patient_calls.model import AqiReading, PatientCall
from reports_service.patient_calls.schema import PaginatedPatientCalls, PatientCallCreate, PatientCallOut, PatientCallUpdate
from reports_service.patients import repository as patient_repo

router = APIRouter(prefix="/patient-calls", tags=["patient-calls"])

_DETAIL_EXAMPLE = PatientCallOut.model_config["json_schema_extra"]["example"]
_LIST_EXAMPLE = {"items": [_DETAIL_EXAMPLE], "page": 1, "page_size": 20, "total": 1}


def _to_out(call: PatientCall) -> PatientCallOut:
    return PatientCallOut(
        call_id=call.call_id,
        patient_id=call.patient_id,
        phone_number=call.phone_number,
        city=call.city,
        condition=call.condition,
        direction=call.direction,
        live_aqi_reading=call.live_aqi_reading.model_dump(),
        second_aqi_reading=call.second_aqi_reading.model_dump() if call.second_aqi_reading else None,
        risk_threshold=call.risk_threshold,
        aqi_reconciliation_delta=call.aqi_reconciliation_delta,
        reading_timestamp=call.reading_timestamp,
        status=call.status,
        disposition=call.disposition,
        telephony_triggered=call.telephony_triggered,
        created_at=call.created_at,
        updated_at=call.updated_at,
    )


async def _resolve_call_in_project(call_id: str, project_id: str) -> PatientCall:
    """Fetches a call by its external `call_id` and verifies it belongs to a
    patient in `project_id`, raising 404 either way (missing call, or a real
    call whose patient is in a different project — never distinguished, so
    this can't be used to probe another project's data).
    """

    call = await call_repo.get_by_call_id(call_id)
    if call is None:
        raise NotFoundError("Patient call not found")
    patient = await patient_repo.get_by_patient_id(call.patient_id, project_id)
    if patient is None:
        raise NotFoundError("Patient call not found")
    return call


@router.get(
    "",
    response_model=SuccessResponse[PaginatedPatientCalls],
    summary="List a project's patient advisory calls",
    description="""Returns a paginated, most-recent-first list of AQI-triggered advisory calls for a
project's patients.

**Required inputs:** `project_id`. `patient_id` optionally narrows to one patient.

**Errors:** 403 if the caller isn't permitted to view `project_id`; 404 if `patient_id` is given but isn't
a patient in `project_id`. Requires `patient_call.view`.""",
    responses=success_and_error_responses("One page of patient calls", _LIST_EXAMPLE, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE),
)
async def list_patient_calls(
    project_id: str = Query(..., description="The project (Mongo ObjectId) to list advisory calls for.", examples=["6ab4ceafe9f5e5190f56b7e9"]),
    patient_id: str | None = Query(default=None, description="Narrow to one patient's calls.", examples=["PT-0003"]),
    page: int = Query(default=1, ge=1),
    page_size: int | None = Query(default=None, ge=1),
    identity: IdentityContext = Depends(require_permission("patient_call.view")),
) -> dict:
    ensure_project_access(identity, project_id)
    if patient_id is not None:
        if await patient_repo.get_by_patient_id(patient_id, project_id) is None:
            raise NotFoundError("Patient not found")
        patient_ids = [patient_id]
    else:
        patient_ids = await patient_repo.list_patient_ids_for_project(project_id)

    settings = get_settings()
    effective_page_size = min(page_size or settings.default_page_size, settings.max_page_size)
    items, total = await call_repo.list_paginated(patient_ids, patient_id, page, effective_page_size)
    return success_response(
        data=PaginatedPatientCalls(items=[_to_out(c) for c in items], page=page, page_size=effective_page_size, total=total)
    )


@router.get(
    "/{call_id}",
    response_model=SuccessResponse[PatientCallOut],
    summary="Get one patient advisory call",
    description="""Returns a single advisory-call record.

**Errors:** 403 if the caller isn't permitted to view `project_id`; 404 if no call with that `call_id`
exists within `project_id`. Requires `patient_call.view`.""",
    responses=success_and_error_responses("Patient call found", _DETAIL_EXAMPLE, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE, not_found_example("Patient call")),
)
async def get_patient_call(
    call_id: str = Path(..., examples=["4ba506a9-8af0-4415-8d82-9fde7e7215f1"]),
    project_id: str = Query(..., examples=["6ab4ceafe9f5e5190f56b7e9"]),
    identity: IdentityContext = Depends(require_permission("patient_call.view")),
) -> dict:
    ensure_project_access(identity, project_id)
    call = await _resolve_call_in_project(call_id, project_id)
    return success_response(data=_to_out(call))


@router.post(
    "",
    response_model=SuccessResponse[PatientCallOut],
    summary="Record a patient advisory call",
    description="""Creates a new advisory-call record. `call_id` is the external telephony/AQI system's own
call identifier — required, and must be unique.

**Required inputs:** `call_id`, `patient_id`, `project_id` (to authorize and to verify `patient_id` belongs
to it), `phone_number`, `city`, `condition`, `direction`, `live_aqi_reading`, `risk_threshold`,
`reading_timestamp`, `status`.

**Errors:** 403 if the caller isn't permitted or isn't assigned to `project_id`; 404 if `patient_id` isn't a
patient in `project_id`; 409 if `call_id` already exists. Requires `patient_call.create`.""",
    responses=success_and_error_responses("Patient call recorded", _DETAIL_EXAMPLE, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE),
)
async def create_patient_call(
    body: PatientCallCreate,
    identity: IdentityContext = Depends(require_permission("patient_call.create")),
) -> dict:
    ensure_project_access(identity, body.project_id)
    if await patient_repo.get_by_patient_id(body.patient_id, body.project_id) is None:
        raise NotFoundError("Patient not found")

    try:
        call = await call_repo.create(
            call_id=body.call_id,
            patient_id=body.patient_id,
            phone_number=body.phone_number,
            city=body.city,
            condition=body.condition,
            direction=body.direction,
            live_aqi_reading=AqiReading(**body.live_aqi_reading.model_dump()),
            second_aqi_reading=AqiReading(**body.second_aqi_reading.model_dump()) if body.second_aqi_reading else None,
            risk_threshold=body.risk_threshold,
            aqi_reconciliation_delta=body.aqi_reconciliation_delta,
            reading_timestamp=body.reading_timestamp,
            status=body.status,
            disposition=body.disposition,
            telephony_triggered=body.telephony_triggered,
        )
    except ValueError as exc:
        raise ConflictError(str(exc)) from exc
    return success_response(data=_to_out(call))


@router.patch(
    "/{call_id}",
    response_model=SuccessResponse[PatientCallOut],
    summary="Update a patient advisory call",
    description="""Partially updates an advisory-call record — only the fields supplied are changed.
Typically used to attach `second_aqi_reading`/`aqi_reconciliation_delta` once reconciliation runs, or to
update `status`/`disposition`/`telephony_triggered`.

**Errors:** 403 if the caller isn't permitted or isn't assigned to `project_id`; 404 if no call with that
`call_id` exists within `project_id`. Requires `patient_call.update`.""",
    responses=success_and_error_responses("Patient call updated", _DETAIL_EXAMPLE, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE, not_found_example("Patient call")),
)
async def update_patient_call(
    body: PatientCallUpdate,
    call_id: str = Path(..., examples=["4ba506a9-8af0-4415-8d82-9fde7e7215f1"]),
    project_id: str = Query(..., examples=["6ab4ceafe9f5e5190f56b7e9"]),
    identity: IdentityContext = Depends(require_permission("patient_call.update")),
) -> dict:
    ensure_project_access(identity, project_id)
    call = await _resolve_call_in_project(call_id, project_id)
    fields = body.model_dump(exclude={"second_aqi_reading"})
    call = await call_repo.update(
        call,
        second_aqi_reading=AqiReading(**body.second_aqi_reading.model_dump()) if body.second_aqi_reading else None,
        **fields,
    )
    return success_response(data=_to_out(call))


@router.delete(
    "/{call_id}",
    response_model=SuccessResponse[dict],
    summary="Remove a patient advisory call",
    description="""Permanently removes an advisory-call record.

**Errors:** 403 if the caller isn't permitted or isn't assigned to `project_id`; 404 if no call with that
`call_id` exists within `project_id`. Requires `patient_call.delete`.""",
    responses=success_and_error_responses("Patient call removed", {"deleted": True}, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE, not_found_example("Patient call")),
)
async def delete_patient_call(
    call_id: str = Path(..., examples=["4ba506a9-8af0-4415-8d82-9fde7e7215f1"]),
    project_id: str = Query(..., examples=["6ab4ceafe9f5e5190f56b7e9"]),
    identity: IdentityContext = Depends(require_permission("patient_call.delete")),
) -> dict:
    ensure_project_access(identity, project_id)
    call = await _resolve_call_in_project(call_id, project_id)
    await call_repo.delete(call)
    return success_response(data={"deleted": True})
