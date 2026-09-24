from datetime import datetime, timezone

from reports_service.risk_thresholds.model import RiskThreshold


async def list_all() -> list[RiskThreshold]:
    return await RiskThreshold.find().sort("condition").to_list()


async def get_by_condition(condition: str) -> RiskThreshold | None:
    return await RiskThreshold.find_one(RiskThreshold.condition == condition)


async def create(*, condition: str, risk_threshold_aqi: int, pollutant: str, is_seeded: bool = False) -> RiskThreshold:
    threshold = RiskThreshold(condition=condition, risk_threshold_aqi=risk_threshold_aqi, pollutant=pollutant, is_seeded=is_seeded)
    await threshold.insert()
    return threshold


async def update(threshold: RiskThreshold, **fields) -> RiskThreshold:
    for key, value in fields.items():
        if value is not None:
            setattr(threshold, key, value)
    threshold.updated_at = datetime.now(timezone.utc)
    await threshold.save()
    return threshold


async def delete(threshold: RiskThreshold) -> None:
    await threshold.delete()
