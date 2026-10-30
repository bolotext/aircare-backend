"""Persisted result of the fixed scenario checklist (see
scripts/run_scenario_checks.py, added in Phase 4) that backs the dashboard's
"scenario measure". This module only defines storage; nothing writes a
ScenarioRun yet until Phase 4 adds the internal POST endpoint.
"""

from datetime import datetime

from beanie import Document
from pydantic import BaseModel
from pymongo import IndexModel


class ScenarioCheck(BaseModel):
    name: str
    passed: bool
    details: str | None = None


class ScenarioRun(Document):
    run_at: datetime
    checks: list[ScenarioCheck]
    passed_count: int
    total_count: int
    scenario_measure_pct: float

    class Settings:
        name = "scenario_runs"
        indexes = [IndexModel([("run_at", -1)])]
