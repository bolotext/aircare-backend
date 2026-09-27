"""Hardcoded demo dataset for the AQI outbound scheduler: two projects and a
40-patient roster split across them, plus the condition -> AQI risk-threshold
reference table.

Fixed synthetic _ids (not Mongo-assigned) so PATIENTS below can reference a
real project _id directly, without a discovery round-trip through the Users
service. Raw motor writes, no Beanie — this is a separate deployable service
and shouldn't import reports_service's/users_service's packages (see
aqi_outbound_scheduler.py's own module docstring for why).

Idempotent per collection, safe to re-run:
    - projects:        skipped if a doc with that fixed _id already exists.
    - patients:         skipped if a doc with that patient_id already exists.
    - risk_thresholds: skipped if a doc with that condition already exists —
                        this one matters in practice: it means a threshold
                        someone has since tuned via the risk-threshold admin
                        feature is never silently overwritten by re-seeding.

Run with:
    make seed-scheduler-demo
    (or: cd services/scheduler && uv run python seed.py)
"""

import asyncio
from datetime import datetime, timezone

from bson import ObjectId

import aqi_outbound_scheduler as scheduler

logger = scheduler.logger


def _dt(iso_timestamp: str) -> datetime:
    return datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))


PROJECT_A_ID = "6ab4ceafe9f5e5190f56b7f0"
PROJECT_B_ID = "6ab4ceafe9f5e5190f56b7f1"
_ORGANIZATION_ID = "6ab4ceafe9f5e5190f56b7e8"
_CREATED_BY = "6ab4ceafe9f5e5190f56b7e7"

PROJECTS = [
    {
        "_id": PROJECT_A_ID,
        "organization_id": _ORGANIZATION_ID,
        "name": "Demo Project A",
        "code": "proj-a",
        "report_db_name": "aircare",
        "report_collection_name": "call_logs",
        "status": "active",
        "created_by": _CREATED_BY,
        "is_seeded": True,
        "created_at": "2026-09-24T07:18:07.801Z",
        "updated_at": "2026-09-24T07:18:07.801Z",
    },
    {
        "_id": PROJECT_B_ID,
        "organization_id": _ORGANIZATION_ID,
        "name": "Demo Project B",
        "code": "proj-b",
        "report_db_name": "aircare",
        "report_collection_name": "call_logs",
        "status": "active",
        "created_by": _CREATED_BY,
        "is_seeded": True,
        "created_at": "2026-09-24T07:18:07.801Z",
        "updated_at": "2026-09-24T07:18:07.801Z",
    },
]

PATIENTS = [
    {
        "patient_id": "PT-0001",
        "full_name": "Synthetic Test Name 01",
        "phone_number": "+910000000001",
        "dob": "1985-04-12",
        "city": "Delhi",
        "condition": "asthma",
        "is_seeded": True,
        "risk_flag": True,
        "preferred_language": "hindi",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T10:00:00Z",
        "project_id": PROJECT_A_ID,
    },
    {
        "patient_id": "PT-0002",
        "full_name": "Synthetic Test Name 02",
        "phone_number": "+910000000002",
        "dob": "1990-11-03",
        "city": "Mumbai",
        "condition": "asthma",
        "is_seeded": True,
        "risk_flag": False,
        "preferred_language": "kannada",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T10:05:00Z",
        "project_id": PROJECT_B_ID,
    },
    {
        "patient_id": "PT-0003",
        "full_name": "Narendra Singh Dangi",
        "phone_number": "+917389058485",
        "dob": "2001-03-31",
        "city": "Bengaluru",
        "condition": "copd",
        "is_seeded": True,
        "risk_flag": True,
        "preferred_language": "hindi",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T10:10:00Z",
        "project_id": PROJECT_A_ID,
    },
    {
        "patient_id": "PT-0004",
        "full_name": "Synthetic Test Name 04",
        "phone_number": "+910000000004",
        "dob": "1968-02-28",
        "city": "Hyderabad",
        "condition": "copd",
        "is_seeded": True,
        "risk_flag": False,
        "preferred_language": "bengali",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T10:15:00Z",
        "project_id": PROJECT_B_ID,
    },
    {
        "patient_id": "PT-0005",
        "full_name": "Synthetic Test Name 05",
        "phone_number": "+910000000005",
        "dob": "1995-09-09",
        "city": "Kolkata",
        "condition": "bronchitis",
        "is_seeded": True,
        "risk_flag": True,
        "preferred_language": "marathi",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T10:20:00Z",
        "project_id": PROJECT_A_ID,
    },
    {
        "patient_id": "PT-0006",
        "full_name": "Synthetic Test Name 06",
        "phone_number": "7389058485",
        "dob": "2001-03-31",
        "city": "Delhi",
        "condition": "bronchitis",
        "is_seeded": True,
        "risk_flag": True,
        "preferred_language": "hindi",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T10:25:00Z",
        "project_id": PROJECT_B_ID,
    },
    {
        "patient_id": "PT-0007",
        "full_name": "Synthetic Test Name 07",
        "phone_number": "+910000000007",
        "dob": "1976-07-07",
        "city": "Delhi",
        "condition": "asthma",
        "is_seeded": True,
        "risk_flag": False,
        "preferred_language": "kannada",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T10:30:00Z",
        "project_id": PROJECT_A_ID,
    },
    {
        "patient_id": "PT-0008",
        "full_name": "Synthetic Test Name 08",
        "phone_number": "+910000000008",
        "dob": "1977-08-08",
        "city": "Mumbai",
        "condition": "copd",
        "is_seeded": True,
        "risk_flag": True,
        "preferred_language": "tamil",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T10:35:00Z",
        "project_id": PROJECT_B_ID,
    },
    {
        "patient_id": "PT-0009",
        "full_name": "Synthetic Test Name 09",
        "phone_number": "+910000000009",
        "dob": "1978-09-09",
        "city": "Bengaluru",
        "condition": "bronchitis",
        "is_seeded": True,
        "risk_flag": True,
        "preferred_language": "bengali",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T10:40:00Z",
        "project_id": PROJECT_A_ID,
    },
    {
        "patient_id": "PT-0010",
        "full_name": "Synthetic Test Name 10",
        "phone_number": "+910000000010",
        "dob": "1979-10-10",
        "city": "Hyderabad",
        "condition": "asthma",
        "is_seeded": True,
        "risk_flag": False,
        "preferred_language": "marathi",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T10:45:00Z",
        "project_id": PROJECT_B_ID,
    },
    {
        "patient_id": "PT-0011",
        "full_name": "Synthetic Test Name 11",
        "phone_number": "+910000000011",
        "dob": "1980-11-11",
        "city": "Kolkata",
        "condition": "copd",
        "is_seeded": True,
        "risk_flag": True,
        "preferred_language": "hindi",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T10:50:00Z",
        "project_id": PROJECT_A_ID,
    },
    {
        "patient_id": "PT-0012",
        "full_name": "Synthetic Test Name 12",
        "phone_number": "+910000000012",
        "dob": "1981-12-12",
        "city": "Chennai",
        "condition": "bronchitis",
        "is_seeded": True,
        "risk_flag": True,
        "preferred_language": "kannada",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T10:55:00Z",
        "project_id": PROJECT_B_ID,
    },
    {
        "patient_id": "PT-0013",
        "full_name": "Synthetic Test Name 13",
        "phone_number": "+910000000013",
        "dob": "1982-01-13",
        "city": "Delhi",
        "condition": "asthma",
        "is_seeded": True,
        "risk_flag": False,
        "preferred_language": "tamil",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T11:00:00Z",
        "project_id": PROJECT_A_ID,
    },
    {
        "patient_id": "PT-0014",
        "full_name": "Synthetic Test Name 14",
        "phone_number": "+910000000014",
        "dob": "1983-02-14",
        "city": "Mumbai",
        "condition": "copd",
        "is_seeded": True,
        "risk_flag": True,
        "preferred_language": "bengali",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T11:05:00Z",
        "project_id": PROJECT_B_ID,
    },
    {
        "patient_id": "PT-0015",
        "full_name": "Synthetic Test Name 15",
        "phone_number": "+910000000015",
        "dob": "1984-03-15",
        "city": "Bengaluru",
        "condition": "bronchitis",
        "is_seeded": True,
        "risk_flag": True,
        "preferred_language": "marathi",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T11:10:00Z",
        "project_id": PROJECT_A_ID,
    },
    {
        "patient_id": "PT-0016",
        "full_name": "Synthetic Test Name 16",
        "phone_number": "+910000000016",
        "dob": "1985-04-16",
        "city": "Hyderabad",
        "condition": "asthma",
        "is_seeded": True,
        "risk_flag": False,
        "preferred_language": "hindi",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T11:15:00Z",
        "project_id": PROJECT_B_ID,
    },
    {
        "patient_id": "PT-0017",
        "full_name": "Synthetic Test Name 17",
        "phone_number": "+910000000017",
        "dob": "1986-05-17",
        "city": "Kolkata",
        "condition": "copd",
        "is_seeded": True,
        "risk_flag": True,
        "preferred_language": "kannada",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T11:20:00Z",
        "project_id": PROJECT_A_ID,
    },
    {
        "patient_id": "PT-0018",
        "full_name": "Synthetic Test Name 18",
        "phone_number": "+910000000018",
        "dob": "1987-06-18",
        "city": "Chennai",
        "condition": "bronchitis",
        "is_seeded": True,
        "risk_flag": True,
        "preferred_language": "tamil",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T11:25:00Z",
        "project_id": PROJECT_B_ID,
    },
    {
        "patient_id": "PT-0019",
        "full_name": "Synthetic Test Name 19",
        "phone_number": "+910000000019",
        "dob": "1988-07-19",
        "city": "Delhi",
        "condition": "asthma",
        "is_seeded": True,
        "risk_flag": False,
        "preferred_language": "bengali",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T11:30:00Z",
        "project_id": PROJECT_A_ID,
    },
    {
        "patient_id": "PT-0020",
        "full_name": "Synthetic Test Name 20",
        "phone_number": "+910000000020",
        "dob": "1989-08-20",
        "city": "Mumbai",
        "condition": "copd",
        "is_seeded": True,
        "risk_flag": True,
        "preferred_language": "marathi",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T11:35:00Z",
        "project_id": PROJECT_B_ID,
    },
    {
        "patient_id": "PT-0021",
        "full_name": "Synthetic Test Name 21",
        "phone_number": "+910000000021",
        "dob": "1990-09-21",
        "city": "Bengaluru",
        "condition": "bronchitis",
        "is_seeded": True,
        "risk_flag": True,
        "preferred_language": "hindi",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T11:40:00Z",
        "project_id": PROJECT_A_ID,
    },
    {
        "patient_id": "PT-0022",
        "full_name": "Synthetic Test Name 22",
        "phone_number": "+910000000022",
        "dob": "1991-10-22",
        "city": "Hyderabad",
        "condition": "asthma",
        "is_seeded": True,
        "risk_flag": False,
        "preferred_language": "kannada",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T11:45:00Z",
        "project_id": PROJECT_B_ID,
    },
    {
        "patient_id": "PT-0023",
        "full_name": "Synthetic Test Name 23",
        "phone_number": "+910000000023",
        "dob": "1992-11-23",
        "city": "Kolkata",
        "condition": "copd",
        "is_seeded": True,
        "risk_flag": True,
        "preferred_language": "tamil",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T11:50:00Z",
        "project_id": PROJECT_A_ID,
    },
    {
        "patient_id": "PT-0024",
        "full_name": "Synthetic Test Name 24",
        "phone_number": "+910000000024",
        "dob": "1993-12-24",
        "city": "Chennai",
        "condition": "bronchitis",
        "is_seeded": True,
        "risk_flag": True,
        "preferred_language": "bengali",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T11:55:00Z",
        "project_id": PROJECT_B_ID,
    },
    {
        "patient_id": "PT-0025",
        "full_name": "Synthetic Test Name 25",
        "phone_number": "+910000000025",
        "dob": "1994-01-25",
        "city": "Delhi",
        "condition": "asthma",
        "is_seeded": True,
        "risk_flag": False,
        "preferred_language": "marathi",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T12:00:00Z",
        "project_id": PROJECT_A_ID,
    },
    {
        "patient_id": "PT-0026",
        "full_name": "Synthetic Test Name 26",
        "phone_number": "+910000000026",
        "dob": "1995-02-26",
        "city": "Mumbai",
        "condition": "copd",
        "is_seeded": True,
        "risk_flag": True,
        "preferred_language": "hindi",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T12:05:00Z",
        "project_id": PROJECT_B_ID,
    },
    {
        "patient_id": "PT-0027",
        "full_name": "Synthetic Test Name 27",
        "phone_number": "+910000000027",
        "dob": "1996-03-27",
        "city": "Bengaluru",
        "condition": "bronchitis",
        "is_seeded": True,
        "risk_flag": True,
        "preferred_language": "kannada",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T12:10:00Z",
        "project_id": PROJECT_A_ID,
    },
    {
        "patient_id": "PT-0028",
        "full_name": "Synthetic Test Name 28",
        "phone_number": "+910000000028",
        "dob": "1997-04-01",
        "city": "Hyderabad",
        "condition": "asthma",
        "is_seeded": True,
        "risk_flag": False,
        "preferred_language": "tamil",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T12:15:00Z",
        "project_id": PROJECT_B_ID,
    },
    {
        "patient_id": "PT-0029",
        "full_name": "Synthetic Test Name 29",
        "phone_number": "+910000000029",
        "dob": "1998-05-02",
        "city": "Kolkata",
        "condition": "copd",
        "is_seeded": True,
        "risk_flag": True,
        "preferred_language": "bengali",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T12:20:00Z",
        "project_id": PROJECT_A_ID,
    },
    {
        "patient_id": "PT-0030",
        "full_name": "Synthetic Test Name 30",
        "phone_number": "+910000000030",
        "dob": "1999-06-03",
        "city": "Chennai",
        "condition": "bronchitis",
        "is_seeded": True,
        "risk_flag": True,
        "preferred_language": "marathi",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T12:25:00Z",
        "project_id": PROJECT_B_ID,
    },
    {
        "patient_id": "PT-0031",
        "full_name": "Synthetic Test Name 31",
        "phone_number": "+910000000031",
        "dob": "1970-07-04",
        "city": "Delhi",
        "condition": "asthma",
        "is_seeded": True,
        "risk_flag": False,
        "preferred_language": "hindi",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T12:30:00Z",
        "project_id": PROJECT_A_ID,
    },
    {
        "patient_id": "PT-0032",
        "full_name": "Synthetic Test Name 32",
        "phone_number": "+910000000032",
        "dob": "1971-08-05",
        "city": "Mumbai",
        "condition": "copd",
        "is_seeded": True,
        "risk_flag": True,
        "preferred_language": "kannada",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T12:35:00Z",
        "project_id": PROJECT_B_ID,
    },
    {
        "patient_id": "PT-0033",
        "full_name": "Synthetic Test Name 33",
        "phone_number": "+910000000033",
        "dob": "1972-09-06",
        "city": "Bengaluru",
        "condition": "bronchitis",
        "is_seeded": True,
        "risk_flag": True,
        "preferred_language": "tamil",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T12:40:00Z",
        "project_id": PROJECT_A_ID,
    },
    {
        "patient_id": "PT-0034",
        "full_name": "Synthetic Test Name 34",
        "phone_number": "+910000000034",
        "dob": "1973-10-07",
        "city": "Hyderabad",
        "condition": "asthma",
        "is_seeded": True,
        "risk_flag": False,
        "preferred_language": "bengali",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T12:45:00Z",
        "project_id": PROJECT_B_ID,
    },
    {
        "patient_id": "PT-0035",
        "full_name": "Synthetic Test Name 35",
        "phone_number": "+910000000035",
        "dob": "1974-11-08",
        "city": "Kolkata",
        "condition": "copd",
        "is_seeded": True,
        "risk_flag": True,
        "preferred_language": "marathi",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T12:50:00Z",
        "project_id": PROJECT_A_ID,
    },
    {
        "patient_id": "PT-0036",
        "full_name": "Synthetic Test Name 36",
        "phone_number": "+910000000036",
        "dob": "1975-12-09",
        "city": "Chennai",
        "condition": "bronchitis",
        "is_seeded": True,
        "risk_flag": True,
        "preferred_language": "hindi",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T12:55:00Z",
        "project_id": PROJECT_B_ID,
    },
    {
        "patient_id": "PT-0037",
        "full_name": "Synthetic Test Name 37",
        "phone_number": "+910000000037",
        "dob": "1976-01-10",
        "city": "Delhi",
        "condition": "asthma",
        "is_seeded": True,
        "risk_flag": False,
        "preferred_language": "kannada",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T13:00:00Z",
        "project_id": PROJECT_A_ID,
    },
    {
        "patient_id": "PT-0038",
        "full_name": "Synthetic Test Name 38",
        "phone_number": "+910000000038",
        "dob": "1977-02-11",
        "city": "Mumbai",
        "condition": "copd",
        "is_seeded": True,
        "risk_flag": True,
        "preferred_language": "tamil",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T13:05:00Z",
        "project_id": PROJECT_B_ID,
    },
    {
        "patient_id": "PT-0039",
        "full_name": "Synthetic Test Name 39",
        "phone_number": "+910000000039",
        "dob": "1978-03-12",
        "city": "Bengaluru",
        "condition": "bronchitis",
        "is_seeded": True,
        "risk_flag": True,
        "preferred_language": "bengali",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T13:10:00Z",
        "project_id": PROJECT_A_ID,
    },
    {
        "patient_id": "PT-0040",
        "full_name": "Synthetic Test Name 40",
        "phone_number": "+910000000040",
        "dob": "1979-04-13",
        "city": "Hyderabad",
        "condition": "asthma",
        "is_seeded": True,
        "risk_flag": False,
        "preferred_language": "marathi",
        "do_not_call": False,
        "last_advisory_episode": None,
        "created_at": "2026-09-01T13:15:00Z",
        "project_id": PROJECT_B_ID,
    },
]

RISK_THRESHOLDS = [
    {"condition": "asthma", "risk_threshold_aqi": 150, "pollutant": "pm2_5", "is_seeded": True},
    {"condition": "copd", "risk_threshold_aqi": 120, "pollutant": "pm2_5", "is_seeded": True},
    {"condition": "bronchitis", "risk_threshold_aqi": 140, "pollutant": "pm2_5", "is_seeded": True},
]


async def seed_projects(db) -> int:
    count = 0
    for project in PROJECTS:
        _id = ObjectId(project["_id"])
        if await db.projects.find_one({"_id": _id}):
            continue
        doc = {
            **project,
            "_id": _id,
            "organization_id": ObjectId(project["organization_id"]),
            "created_by": ObjectId(project["created_by"]),
            "created_at": _dt(project["created_at"]),
            "updated_at": _dt(project["updated_at"]),
        }
        await db.projects.insert_one(doc)
        count += 1
    return count


async def seed_patients(db) -> int:
    count = 0
    for patient in PATIENTS:
        if await db.patients.find_one({"patient_id": patient["patient_id"]}):
            continue
        doc = {
            **patient,
            "project_id": ObjectId(patient["project_id"]),
            "created_at": _dt(patient["created_at"]),
        }
        await db.patients.insert_one(doc)
        count += 1
    return count


async def seed_risk_thresholds(db) -> int:
    count = 0
    for threshold in RISK_THRESHOLDS:
        if await db.risk_thresholds.find_one({"condition": threshold["condition"]}):
            # Deliberately never overwritten: a threshold tuned since via the
            # risk-threshold admin feature must survive a re-seed untouched.
            continue
        now = datetime.now(timezone.utc)
        await db.risk_thresholds.insert_one({**threshold, "created_at": now, "updated_at": now})
        count += 1
    return count


async def seed() -> None:
    logger.info("seed: %s", scheduler.mongo_config_summary())
    db = scheduler.get_db()
    projects_inserted = await seed_projects(db)
    patients_inserted = await seed_patients(db)
    thresholds_inserted = await seed_risk_thresholds(db)
    logger.info(
        "seed: projects_inserted=%s (of %s) patients_inserted=%s (of %s) risk_thresholds_inserted=%s (of %s)",
        projects_inserted, len(PROJECTS), patients_inserted, len(PATIENTS), thresholds_inserted, len(RISK_THRESHOLDS),
    )


async def _main() -> None:
    try:
        await seed()
    finally:
        scheduler.close_db()


if __name__ == "__main__":
    asyncio.run(_main())
