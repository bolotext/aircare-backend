"""Synthetic call-data seeding. There is no live ingestion pipeline in this
build (see docs/KNOWN_LIMITATIONS.md) — this script is how the two demo
projects get data for the dashboard/export/automailer to report on.

Usage (after `make seed-demo` in services/users has printed project ids):

    cd services/reports
    uv run python main.py seed-calls \
        --organization-id <org id> \
        --project-id <project A id> \
        --project-id <project B id>

Deliberately includes, per project (except the *last* --project-id given,
which is left with zero calls to demonstrate the empty-data dashboard case):
- a spread of dispositions/languages/call statuses/tool-failure counts
- exactly one "incomplete" call (connected but never ended)
- four IST-day-boundary fixtures anchored to --date (default: yesterday in
  Asia/Kolkata) for the automailer's start-inclusive/end-exclusive window
  tests: the reference day's 00:00:00 and 23:59:59 (both IN that day's
  window), the previous day's 23:59:59, and the next day's 00:00:00 (both
  OUT of that day's window).
"""

import random
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from reports_service.calls import repository as call_repo

IST = ZoneInfo("Asia/Kolkata")
LANGUAGES = ["en-IN", "hi-IN", "ta-IN", "te-IN", "kn-IN"]
DISPOSITIONS = [
    "RESOLVED",
    "APPOINTMENT_BOOKED",
    "INFO_PROVIDED",
    "TRANSFERRED_SUCCESSFULLY",
    "ABANDONED",
    "NO_ANSWER",
    "VOICEMAIL",
    "TECHNICAL_FAILURE",
    "NEUTRAL",
]


def _ist_midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=IST)


def _make_answered_payload(
    conversation_id: str, project_id: str, organization_id: str, language: str, disposition: str,
    start: datetime, tool_failure_count: int, incomplete: bool = False,
) -> dict:
    connected = start + timedelta(seconds=random.randint(3, 12))
    duration = random.randint(30, 300)
    end = None if incomplete else connected + timedelta(seconds=duration)
    return {
        "conversation_id": conversation_id,
        "project_id": project_id,
        "organization_id": organization_id,
        "bot_id": "seed-bot",
        "language": language,
        "call_status": random.choice(["ANSWERED", "COMPLETED"]),
        "disposition": disposition,
        "call_start_time": start,
        "call_connected_time": connected,
        "call_end_time": end,
        "connected_seconds": None if end is None else (end - connected).total_seconds(),
        "tool_failure_count": tool_failure_count,
        "raw": {"simulated": True, "conversation_log": []},
    }


def _make_unanswered_payload(
    conversation_id: str, project_id: str, organization_id: str, language: str, start: datetime,
) -> dict:
    status = random.choice(["NO_ANSWER", "FAILED"])
    return {
        "conversation_id": conversation_id,
        "project_id": project_id,
        "organization_id": organization_id,
        "bot_id": "seed-bot",
        "language": language,
        "call_status": status,
        "disposition": "NO_ANSWER" if status == "NO_ANSWER" else "TECHNICAL_FAILURE",
        "call_start_time": start,
        "call_connected_time": None,
        "call_end_time": None,
        "connected_seconds": None,
        "tool_failure_count": 0,
        "raw": {"simulated": True, "conversation_log": []},
    }


def _boundary_payloads(project_id: str, organization_id: str, reference_day: date) -> list[dict]:
    prev_day = reference_day - timedelta(days=1)
    next_day = reference_day + timedelta(days=1)
    fixtures = [
        ("boundary-midnight-start", _ist_midnight(reference_day)),  # IN window
        ("boundary-day-end", _ist_midnight(reference_day) + timedelta(hours=23, minutes=59, seconds=59)),  # IN window
        ("boundary-prev-day-end", _ist_midnight(prev_day) + timedelta(hours=23, minutes=59, seconds=59)),  # OUT
        ("boundary-next-day-start", _ist_midnight(next_day)),  # OUT
    ]
    payloads = []
    for suffix, start_ist in fixtures:
        start_utc = start_ist.astimezone(timezone.utc)
        payloads.append(
            _make_answered_payload(
                conversation_id=f"seed-{project_id}-{suffix}",
                project_id=project_id,
                organization_id=organization_id,
                language=random.choice(LANGUAGES),
                disposition="RESOLVED",
                start=start_utc,
                tool_failure_count=0,
            )
        )
    return payloads


async def seed_calls_for_project(
    project_id: str, organization_id: str, reference_day: date, calls_per_project: int = 30
) -> int:
    random.seed(f"{project_id}-seed")  # deterministic across re-runs
    count = 0

    for i in range(calls_per_project):
        language = random.choice(LANGUAGES)
        start = _ist_midnight(reference_day).astimezone(timezone.utc) + timedelta(
            hours=random.randint(0, 22), minutes=random.randint(0, 59)
        )
        conversation_id = f"seed-{project_id}-call-{i:04d}"
        if random.random() < 0.75:
            disposition = random.choice(DISPOSITIONS)
            tool_failure_count = random.choices([0, 0, 0, 1, 2], k=1)[0]
            payload = _make_answered_payload(
                conversation_id, project_id, organization_id, language, disposition, start, tool_failure_count
            )
        else:
            payload = _make_unanswered_payload(conversation_id, project_id, organization_id, language, start)
        await call_repo.upsert_call_from_payload(payload)
        count += 1

    # Exactly one incomplete call (connected, never ended).
    incomplete_start = _ist_midnight(reference_day).astimezone(timezone.utc) + timedelta(hours=10)
    await call_repo.upsert_call_from_payload(
        _make_answered_payload(
            f"seed-{project_id}-incomplete", project_id, organization_id, random.choice(LANGUAGES),
            "NEUTRAL", incomplete_start, 0, incomplete=True,
        )
    )
    count += 1

    for payload in _boundary_payloads(project_id, organization_id, reference_day):
        await call_repo.upsert_call_from_payload(payload)
        count += 1

    return count


async def seed_calls(
    project_ids: list[str], organization_id: str, reference_day: date | None = None, calls_per_project: int = 30
) -> None:
    if not project_ids:
        raise ValueError("At least one --project-id is required")

    reference_day = reference_day or (datetime.now(IST).date() - timedelta(days=1))

    # The last project id is deliberately left with zero calls, to exercise
    # the empty-data dashboard/CSV/email path (every ratio must render as
    # N/A, never 0 or an error).
    projects_to_seed = project_ids[:-1] if len(project_ids) > 1 else project_ids
    empty_project = project_ids[-1] if len(project_ids) > 1 else None

    for project_id in projects_to_seed:
        n = await seed_calls_for_project(project_id, organization_id, reference_day, calls_per_project)
        print(f"Seeded {n} calls for project {project_id} (reference day {reference_day}, Asia/Kolkata)")

    if empty_project:
        print(f"Left project {empty_project} with zero calls (empty-data dashboard fixture)")
