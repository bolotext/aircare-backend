"""CSV export used by GET /export.csv and (Phase 3) the automailer
attachment — both call build_csv() with the same DashboardMetrics object
compute_metrics() already produced, so the CSV can never numerically diverge
from the dashboard that produced it.
"""

import csv
import io

from reports_service.calls.model import Call
from reports_service.metrics.formatting import format_pct, format_seconds
from reports_service.metrics.schema import DashboardMetrics


def build_csv(calls: list[Call], metrics: DashboardMetrics) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)

    writer.writerow(["Metric", "Value"])
    writer.writerow(["Total calls", metrics.total_calls])
    writer.writerow(["Unique call IDs", metrics.unique_call_ids])
    writer.writerow(["Answered calls", metrics.answered_calls])
    writer.writerow(["Answer rate", format_pct(metrics.answer_rate_pct)])
    writer.writerow(["Avg connected seconds", format_seconds(metrics.avg_connected_seconds)])
    writer.writerow(["Incomplete calls", metrics.incomplete_calls_count])
    writer.writerow(["Successful dispositions", metrics.successful_disposition_count])
    writer.writerow(["Success rate", format_pct(metrics.success_rate_pct)])
    writer.writerow(["Tool failures", metrics.tool_failure_count])
    writer.writerow(["Calls with tool failure", metrics.calls_with_tool_failure])
    writer.writerow(
        [
            "Scenario measure",
            format_pct(metrics.scenario_measure_pct) if metrics.scenario_measure_available else "N/A",
        ]
    )
    for disposition, count in sorted(metrics.dispositions.items()):
        writer.writerow([f"Disposition: {disposition}", count])
    for language, count in sorted(metrics.language_breakdown.items()):
        writer.writerow([f"Language: {language}", count])

    writer.writerow([])
    writer.writerow(
        [
            "conversation_id",
            "project_id",
            "language",
            "call_status",
            "disposition",
            "call_start_time",
            "call_connected_time",
            "call_end_time",
            "connected_seconds",
            "tool_failure_count",
        ]
    )
    for call in calls:
        writer.writerow(
            [
                call.conversation_id,
                str(call.project_id),
                call.language,
                call.call_status,
                call.disposition,
                call.call_start_time.isoformat(),
                call.call_connected_time.isoformat() if call.call_connected_time else "N/A",
                call.call_end_time.isoformat() if call.call_end_time else "N/A",
                call.connected_seconds if call.connected_seconds is not None else "N/A",
                call.tool_failure_count,
            ]
        )

    return buffer.getvalue().encode("utf-8")
