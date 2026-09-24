"""Seeds the fixed condition -> AQI risk-threshold reference table. Not
project-scoped (see risk_thresholds/model.py), so unlike seed_patients this
takes no arguments.

Usage:

    cd services/reports
    uv run python main.py seed-risk-thresholds

Idempotent by `condition`.
"""

from reports_service.risk_thresholds import repository as threshold_repo

_FIXTURE = [
    {"condition": "asthma", "risk_threshold_aqi": 150, "pollutant": "pm2_5"},
    {"condition": "copd", "risk_threshold_aqi": 120, "pollutant": "pm2_5"},
    {"condition": "bronchitis", "risk_threshold_aqi": 140, "pollutant": "pm2_5"},
]


async def seed_risk_thresholds() -> None:
    count = 0
    for fixture in _FIXTURE:
        existing = await threshold_repo.get_by_condition(fixture["condition"])
        if existing is not None:
            continue
        await threshold_repo.create(
            condition=fixture["condition"],
            risk_threshold_aqi=fixture["risk_threshold_aqi"],
            pollutant=fixture["pollutant"],
            is_seeded=True,
        )
        count += 1
    print(f"Seeded {count} risk thresholds")
