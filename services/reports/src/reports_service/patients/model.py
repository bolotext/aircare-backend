"""Patient master data: the roster of patients a project's advisory/outreach
calls are placed against. Project-scoped exactly like `Call` (see calls/model.py)
so a caller only ever sees patients belonging to a project they're assigned
to — enforced the same way, via `core.deps.ensure_project_access`, not by
anything on this model itself. Deliberately carries no `organization_id`
(unlike `Call`): access here is gated purely by project assignment, and
resolving a project's org would mean an extra Users-service round trip on
every create for no authorization benefit.
"""

from datetime import date, datetime, timezone

from beanie import Document, PydanticObjectId
from pydantic import Field
from pymongo import IndexModel


class Patient(Document):
    # Human-friendly business key ("PT-0001"), distinct from Mongo's own
    # `_id` — this is what routes/clients address a patient by, mirroring
    # `Call.conversation_id`.
    patient_id: str
    project_id: PydanticObjectId

    full_name: str
    phone_number: str
    dob: date
    city: str
    condition: str
    preferred_language: str

    risk_flag: bool = False
    do_not_call: bool = False
    last_advisory_episode: datetime | None = None

    # True only for the demo fixture seeded via `seed-patients` — lets the
    # frontend/tests distinguish synthetic rows from ones a real user created.
    is_seeded: bool = False

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "patients"
        indexes = [
            IndexModel("patient_id", unique=True),
            IndexModel([("project_id", 1), ("created_at", -1)]),
            IndexModel([("project_id", 1), ("condition", 1)]),
            IndexModel([("project_id", 1), ("risk_flag", 1)]),
        ]
