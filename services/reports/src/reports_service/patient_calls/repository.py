from datetime import datetime, timezone

from beanie import PydanticObjectId
from bson.errors import InvalidId
from pydantic import ValidationError
from pymongo.errors import DuplicateKeyError

from reports_service.patient_calls.model import PatientCall


async def list_paginated(
    patient_ids: list[str], patient_id: str | None, page: int, page_size: int
) -> tuple[list[PatientCall], int]:
    """`patient_ids` is every patient_id belonging to the project being
    queried (see patients.repository.list_patient_ids_for_project) — this is
    how project scoping is enforced here, since PatientCall carries no
    project_id of its own. `patient_id`, when given, narrows further to one
    patient (still checked against `patient_ids` by the caller before this
    is called, so it can't be used to reach another project's patient).
    """

    query: dict = {"patient_id": {"$in": patient_ids} if patient_id is None else patient_id}
    total = await PatientCall.find(query).count()
    cursor = PatientCall.find(query).sort("-created_at").skip((page - 1) * page_size).limit(page_size)
    items = await cursor.to_list()
    return items, total


async def get_by_call_id(call_id: str) -> PatientCall | None:
    return await PatientCall.find_one(PatientCall.call_id == call_id)


async def get_by_id(id: str) -> PatientCall | None:
    """Looks up by Mongo `_id`. Guards two invalid-but-not-exceptional
    inputs the same way as a genuine miss (404, not 500): a malformed
    ObjectId string, and an id belonging to a native voice `Call` document
    (this collection is shared — see model.py — and those lack `call_id`,
    so parsing one as a `PatientCall` fails required-field validation).
    """

    try:
        object_id = PydanticObjectId(id)
    except (ValueError, TypeError, InvalidId):
        return None
    try:
        return await PatientCall.get(object_id)
    except ValidationError:
        return None


async def create(*, call_id: str, patient_id: str, **fields) -> PatientCall:
    call = PatientCall(call_id=call_id, patient_id=patient_id, **fields)
    try:
        await call.insert()
    except DuplicateKeyError as exc:
        raise ValueError(f"call_id '{call_id}' already exists") from exc
    return call


async def update(call: PatientCall, **fields) -> PatientCall:
    for key, value in fields.items():
        if value is not None:
            setattr(call, key, value)
    call.updated_at = datetime.now(timezone.utc)
    await call.save()
    return call


async def delete(call: PatientCall) -> None:
    await call.delete()
