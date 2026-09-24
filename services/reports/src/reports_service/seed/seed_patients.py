"""Synthetic patient-roster seeding — the fixed 4-patient demo fixture, split
across the two demo projects the same way `seed_calls.py` splits synthetic
call data: two patients per project.

Usage (after `make seed-demo` in services/users has printed project ids):

    cd services/reports
    uv run python main.py seed-patients \
        --project-a-id <project A id> \
        --project-b-id <project B id>

Idempotent by `full_name`+`project_id` (not `patient_id`, which is always
server-generated — see patients.repository._generate_patient_id) — re-running
this command never creates duplicate rows for the same demo fixture.
"""

from datetime import date

from beanie import PydanticObjectId

from reports_service.patients import repository as patient_repo
from reports_service.patients.model import Patient

_FIXTURE = [
    {
        "full_name": "Synthetic Test Name 01",
        "phone_number": "+910000000001",
        "dob": date(1985, 4, 12),
        "city": "Delhi",
        "condition": "asthma",
        "risk_flag": True,
        "preferred_language": "hindi",
        "do_not_call": False,
        "project": "a",
    },
    {
        "full_name": "Synthetic Test Name 02",
        "phone_number": "+910000000002",
        "dob": date(1990, 11, 3),
        "city": "Mumbai",
        "condition": "asthma",
        "risk_flag": False,
        "preferred_language": "kannada",
        "do_not_call": False,
        "project": "b",
    },
    {
        "full_name": "Narendra Singh Dangi",
        "phone_number": "+917389058485",
        "dob": date(2001, 3, 31),
        "city": "Bengaluru",
        "condition": "copd",
        "risk_flag": True,
        "preferred_language": "hindi",
        "do_not_call": False,
        "project": "a",
    },
    {
        "full_name": "Synthetic Test Name 04",
        "phone_number": "+910000000004",
        "dob": date(1968, 2, 28),
        "city": "Hyderabad",
        "condition": "copd",
        "risk_flag": False,
        "preferred_language": "bengali",
        "do_not_call": False,
        "project": "b",
    },
]


async def seed_patients(project_a_id: str, project_b_id: str) -> None:
    project_ids = {"a": project_a_id, "b": project_b_id}
    count = 0
    for fixture in _FIXTURE:
        project_id = project_ids[fixture["project"]]
        existing = await Patient.find_one(
            Patient.full_name == fixture["full_name"], Patient.project_id == PydanticObjectId(project_id)
        )
        if existing is not None:
            continue
        await patient_repo.create_patient(
            project_id=project_id,
            is_seeded=True,
            full_name=fixture["full_name"],
            phone_number=fixture["phone_number"],
            dob=fixture["dob"],
            city=fixture["city"],
            condition=fixture["condition"],
            preferred_language=fixture["preferred_language"],
            risk_flag=fixture["risk_flag"],
            do_not_call=fixture["do_not_call"],
        )
        count += 1
    print(f"Seeded {count} patients (project A: {project_a_id}, project B: {project_b_id})")
