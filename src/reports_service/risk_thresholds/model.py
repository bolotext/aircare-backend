"""Platform-wide config: the pollutant-AQI threshold above which a patient
with a given respiratory condition is considered at risk. Not project-scoped
— unlike `Patient`/`Call`, this is a single shared reference table the
dashboard/advisory logic reads for every project — and only an ADMIN may
manage it (see risk_thresholds/router.py).
"""

from datetime import datetime, timezone

from beanie import Document
from pydantic import Field
from pymongo import IndexModel


class RiskThreshold(Document):
    condition: str
    risk_threshold_aqi: int
    pollutant: str

    # True only for the demo fixture seeded via `seed-risk-thresholds`.
    is_seeded: bool = False

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "risk_thresholds"
        indexes = [IndexModel("condition", unique=True)]
