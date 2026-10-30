from reports_service.scenarios.model import ScenarioRun


async def get_latest_scenario_measure() -> tuple[float | None, bool]:
    """Returns (scenario_measure_pct, available). available=False (and the
    percentage is None) until the first ScenarioRun has ever been recorded —
    dashboard/CSV/email must render "N/A" in that case, not 0%.
    """

    latest = await ScenarioRun.find().sort(-ScenarioRun.run_at).first_or_none()
    if latest is None:
        return None, False
    return latest.scenario_measure_pct, True
