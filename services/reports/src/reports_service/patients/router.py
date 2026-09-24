"""Patient roster CRUD, project-scoped the same way as `/calls` — every
route requires `project_id` and re-checks `ensure_project_access` itself
(never trusted from the permission string alone), so a Supervisor/Viewer can
only ever see or touch patients belonging to a project they're assigned to.
See core.deps for what `ensure_project_access`/`require_permission` do.
"""

from common.errors import NotFoundError
from common.openapi import FORBIDDEN_EXAMPLE, UNAUTHORIZED_EXAMPLE, not_found_example, success_and_error_responses
from common.responses import success_response
from common.schemas import SuccessResponse
from fastapi import APIRouter, Depends, Path, Query

from reports_service.core.config import get_settings
from reports_service.core.deps import ensure_project_access, require_permission
from reports_service.core.users_client import IdentityContext
from reports_service.patients import repository as patient_repo
from reports_service.patients.model import Patient
from reports_service.patients.repository import PatientFilter
from reports_service.patients.schema import PaginatedPatients, PatientCreate, PatientOut, PatientUpdate

router = APIRouter(prefix="/patients", tags=["patients"])

_LIST_EXAMPLE = {
    "items": [PatientOut.model_config["json_schema_extra"]["example"]],
    "page": 1,
    "page_size": 20,
    "total": 4,
}
_DETAIL_EXAMPLE = PatientOut.model_config["json_schema_extra"]["example"]


def _to_out(patient: Patient) -> PatientOut:
    return PatientOut(
        patient_id=patient.patient_id,
        project_id=str(patient.project_id),
        full_name=patient.full_name,
        phone_number=patient.phone_number,
        dob=patient.dob,
        city=patient.city,
        condition=patient.condition,
        preferred_language=patient.preferred_language,
        risk_flag=patient.risk_flag,
        do_not_call=patient.do_not_call,
        last_advisory_episode=patient.last_advisory_episode,
        is_seeded=patient.is_seeded,
        created_at=patient.created_at,
        updated_at=patient.updated_at,
    )


@router.get(
    "",
    response_model=SuccessResponse[PaginatedPatients],
    summary="List patients for a project",
    description="""Returns a paginated, most-recently-added-first roster of patients for one project.

**When to use:** populating the patient roster table, or scanning a project's patients before opening one
to edit.

**Required inputs:** `project_id`. All other query parameters are optional filters.

**Processing behavior:** filters are combined with AND. Requires the `patient.view` permission and access
to `project_id`.

**Errors:** 403 if the caller isn't permitted to view `project_id`; 422 if `project_id` is missing.""",
    responses=success_and_error_responses("One page of patients", _LIST_EXAMPLE, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE),
)
async def list_patients(
    project_id: str = Query(..., description="The project (Mongo ObjectId) to list patients for.", examples=["6aae5deae9a48c4dc62aa398"]),
    condition: str | None = Query(default=None, description="Exact-match condition filter, e.g. `asthma`.", examples=["asthma"]),
    risk_flag: bool | None = Query(default=None, description="Filter to only risk-flagged (or only non-risk-flagged) patients."),
    do_not_call: bool | None = Query(default=None, description="Filter to only do-not-call (or only callable) patients."),
    page: int = Query(default=1, ge=1, description="1-indexed page number to return."),
    page_size: int | None = Query(
        default=None, ge=1,
        description="Items per page. Defaults to the service's configured default and is capped at its configured maximum.",
        examples=[20],
    ),
    identity: IdentityContext = Depends(require_permission("patient.view")),
) -> dict:
    ensure_project_access(identity, project_id)
    settings = get_settings()
    effective_page_size = min(page_size or settings.default_page_size, settings.max_page_size)

    patient_filter = PatientFilter(project_id=project_id, condition=condition, risk_flag=risk_flag, do_not_call=do_not_call)
    items, total = await patient_repo.list_paginated(patient_filter, page=page, page_size=effective_page_size)
    return success_response(
        data=PaginatedPatients(items=[_to_out(p) for p in items], page=page, page_size=effective_page_size, total=total)
    )


@router.get(
    "/{patient_id}",
    response_model=SuccessResponse[PatientOut],
    summary="Get one patient",
    description="""Returns a single patient record.

**Required inputs:** `patient_id` (path) and `project_id` (query) — both required since `patient_id` is
only guaranteed unique within a project.

**Errors:** 403 if the caller isn't permitted to view `project_id`; 404 if no patient with that
`patient_id` exists within `project_id`.""",
    responses=success_and_error_responses("Patient found", _DETAIL_EXAMPLE, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE, not_found_example("Patient")),
)
async def get_patient(
    patient_id: str = Path(..., description="The patient's business identifier.", examples=["PT-0001"]),
    project_id: str = Query(..., description="The project (Mongo ObjectId) this patient belongs to.", examples=["6aae5deae9a48c4dc62aa398"]),
    identity: IdentityContext = Depends(require_permission("patient.view")),
) -> dict:
    ensure_project_access(identity, project_id)
    patient = await patient_repo.get_by_patient_id(patient_id, project_id)
    if patient is None:
        raise NotFoundError("Patient not found")
    return success_response(data=_to_out(patient))


@router.post(
    "",
    response_model=SuccessResponse[PatientOut],
    summary="Add a patient to a project's roster",
    description="""Creates a new patient record. `patient_id` is always server-generated.

**Required inputs:** `project_id`, `full_name`, `phone_number`, `dob`, `city`, `condition`, `preferred_language`.

**Errors:** 403 if the caller isn't permitted to create patients or isn't assigned to `project_id`; 422 if a
required field is missing or malformed. Requires `patient.create`.""",
    responses=success_and_error_responses("Patient created", _DETAIL_EXAMPLE, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE),
)
async def create_patient(
    body: PatientCreate,
    identity: IdentityContext = Depends(require_permission("patient.create")),
) -> dict:
    ensure_project_access(identity, body.project_id)
    patient = await patient_repo.create_patient(
        project_id=body.project_id,
        full_name=body.full_name,
        phone_number=body.phone_number,
        dob=body.dob,
        city=body.city,
        condition=body.condition,
        preferred_language=body.preferred_language,
        risk_flag=body.risk_flag,
        do_not_call=body.do_not_call,
    )
    return success_response(data=_to_out(patient))


@router.patch(
    "/{patient_id}",
    response_model=SuccessResponse[PatientOut],
    summary="Update a patient",
    description="""Partially updates a patient record — only the fields supplied in the body are changed.

**Required inputs:** `patient_id` (path) and `project_id` (query), to scope the lookup.

**Errors:** 403 if the caller isn't permitted or isn't assigned to `project_id`; 404 if no patient with that
`patient_id` exists within `project_id`. Requires `patient.update`.""",
    responses=success_and_error_responses("Patient updated", _DETAIL_EXAMPLE, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE, not_found_example("Patient")),
)
async def update_patient(
    body: PatientUpdate,
    patient_id: str = Path(..., description="The patient's business identifier.", examples=["PT-0001"]),
    project_id: str = Query(..., description="The project (Mongo ObjectId) this patient belongs to.", examples=["6aae5deae9a48c4dc62aa398"]),
    identity: IdentityContext = Depends(require_permission("patient.update")),
) -> dict:
    ensure_project_access(identity, project_id)
    patient = await patient_repo.get_by_patient_id(patient_id, project_id)
    if patient is None:
        raise NotFoundError("Patient not found")
    patient = await patient_repo.update_patient(patient, **body.model_dump())
    return success_response(data=_to_out(patient))


@router.delete(
    "/{patient_id}",
    response_model=SuccessResponse[dict],
    summary="Remove a patient",
    description="""Permanently removes a patient record from a project's roster.

**Required inputs:** `patient_id` (path) and `project_id` (query), to scope the lookup.

**Errors:** 403 if the caller isn't permitted or isn't assigned to `project_id`; 404 if no patient with that
`patient_id` exists within `project_id`. Requires `patient.delete`.""",
    responses=success_and_error_responses("Patient removed", {"deleted": True}, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE, not_found_example("Patient")),
)
async def delete_patient(
    patient_id: str = Path(..., description="The patient's business identifier.", examples=["PT-0001"]),
    project_id: str = Query(..., description="The project (Mongo ObjectId) this patient belongs to.", examples=["6aae5deae9a48c4dc62aa398"]),
    identity: IdentityContext = Depends(require_permission("patient.delete")),
) -> dict:
    ensure_project_access(identity, project_id)
    patient = await patient_repo.get_by_patient_id(patient_id, project_id)
    if patient is None:
        raise NotFoundError("Patient not found")
    await patient_repo.delete_patient(patient)
    return success_response(data={"deleted": True})
