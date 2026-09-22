from pydantic import BaseModel, ConfigDict, Field


class DashboardMetrics(BaseModel):
    """Aggregate call metrics for a project over the applied filters. Every
    metric definition (answered, successful disposition, ...) is centralized
    in `metrics/service.py`'s module docstring; the dashboard, CSV export,
    and automailer email all render these exact numbers."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "total_calls": 30,
                "unique_call_ids": 30,
                "answered_calls": 22,
                "answer_rate_pct": 73.33,
                "avg_connected_seconds": 189.1,
                "incomplete_calls_count": 1,
                "dispositions": {"RESOLVED": 4, "NO_ANSWER": 6, "ABANDONED": 1},
                "language_breakdown": {"en-IN": 6, "hi-IN": 7, "kn-IN": 8, "ta-IN": 7, "te-IN": 2},
                "successful_disposition_count": 9,
                "success_rate_pct": 30.0,
                "tool_failure_count": 9,
                "calls_with_tool_failure": 8,
                "scenario_measure_pct": None,
                "scenario_measure_available": False,
            }
        }
    )

    total_calls: int = Field(description="Total number of calls matching the applied filters.")
    unique_call_ids: int = Field(description="Distinct `conversation_id` count among matching calls (computed explicitly, not assumed equal to `total_calls`).")

    answered_calls: int = Field(description='Calls with `call_status` in `{"ANSWERED", "COMPLETED"}` AND a non-null `call_connected_time`.')
    answer_rate_pct: float | None = Field(description="`answered_calls / total_calls * 100`, or `null` when `total_calls` is 0 (never 0/NaN).")

    avg_connected_seconds: float | None = Field(description="Mean `connected_seconds` over answered calls where it's set, or `null` when there are none.")
    incomplete_calls_count: int = Field(description="Calls that connected but never got an end time — counted in `total_calls`, excluded from `avg_connected_seconds`.")

    dispositions: dict[str, int] = Field(description="Count of matching calls per `disposition` value.")
    language_breakdown: dict[str, int] = Field(description="Count of matching calls per `language` value.")
    successful_disposition_count: int = Field(description="Calls whose disposition is one of the service's defined successful outcomes (e.g. `RESOLVED`, `APPOINTMENT_BOOKED`).")
    success_rate_pct: float | None = Field(description="`successful_disposition_count / total_calls * 100`, or `null` when `total_calls` is 0.")

    tool_failure_count: int = Field(description="Sum of `tool_failure_count` across matching calls — a count of failed tool invocations, not of affected calls.")
    calls_with_tool_failure: int = Field(description="Number of matching calls with at least one tool failure.")

    scenario_measure_pct: float | None = Field(description="Percentage of the fixed scenario checklist that passed in the most recent recorded run, or `null` if none has run yet.")
    scenario_measure_available: bool = Field(description="Whether `scenario_measure_pct` reflects a real recorded run (`true`) or there's no data yet (`false`).")
