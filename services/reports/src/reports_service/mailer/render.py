"""Renders the daily HTML summary from the exact same `DashboardMetrics`
object the dashboard route and the CSV export use — never a separate
recomputation — so the three surfaces cannot numerically diverge.
"""

from datetime import datetime, timezone

from jinja2 import Template

from reports_service.mailer.window import IST
from reports_service.metrics.formatting import format_pct, format_seconds
from reports_service.metrics.schema import DashboardMetrics

_TEMPLATE = Template(
    """
<html>
  <body style="font-family: sans-serif;">
    <h2>Call Report — {{ scope_label }}</h2>
    <p><strong>Reporting period (Asia/Kolkata):</strong> {{ window_start_ist }} to {{ window_end_ist }} (end-exclusive)</p>
    <p><strong>Generated at (Asia/Kolkata):</strong> {{ generated_at_ist }}</p>

    <table cellpadding="4" cellspacing="0" border="1">
      <tr><td>Total calls</td><td>{{ metrics.total_calls }}</td></tr>
      <tr><td>Unique call IDs</td><td>{{ metrics.unique_call_ids }}</td></tr>
      <tr><td>Answered calls</td><td>{{ metrics.answered_calls }}</td></tr>
      <tr><td>Answer rate</td><td>{{ answer_rate }}</td></tr>
      <tr><td>Avg connected seconds</td><td>{{ avg_connected }}</td></tr>
      <tr><td>Incomplete calls</td><td>{{ metrics.incomplete_calls_count }}</td></tr>
      <tr><td>Successful dispositions</td><td>{{ metrics.successful_disposition_count }}</td></tr>
      <tr><td>Success rate</td><td>{{ success_rate }}</td></tr>
      <tr><td>Tool failures</td><td>{{ metrics.tool_failure_count }}</td></tr>
      <tr><td>Calls with tool failure</td><td>{{ metrics.calls_with_tool_failure }}</td></tr>
      <tr><td>Scenario measure</td><td>{{ scenario_measure }}</td></tr>
    </table>

    <h3>Language breakdown</h3>
    <table cellpadding="4" cellspacing="0" border="1">
      {% for language, count in language_breakdown.items() %}
      <tr><td>{{ language }}</td><td>{{ count }}</td></tr>
      {% endfor %}
    </table>

    <h3>Outcome (disposition) breakdown</h3>
    <table cellpadding="4" cellspacing="0" border="1">
      {% for disposition, count in metrics.dispositions.items() %}
      <tr><td>{{ disposition }}</td><td>{{ count }}</td></tr>
      {% endfor %}
    </table>
  </body>
</html>
"""
)


def render_report_html(
    *,
    scope_label: str,
    window_start: datetime,
    window_end: datetime,
    metrics: DashboardMetrics,
) -> str:
    generated_at_ist = datetime.now(timezone.utc).astimezone(IST)
    return _TEMPLATE.render(
        scope_label=scope_label,
        window_start_ist=window_start.astimezone(IST).isoformat(),
        window_end_ist=window_end.astimezone(IST).isoformat(),
        generated_at_ist=generated_at_ist.isoformat(),
        metrics=metrics,
        language_breakdown=metrics.language_breakdown,
        answer_rate=format_pct(metrics.answer_rate_pct),
        avg_connected=format_seconds(metrics.avg_connected_seconds),
        success_rate=format_pct(metrics.success_rate_pct),
        scenario_measure=format_pct(metrics.scenario_measure_pct) if metrics.scenario_measure_available else "N/A",
    )
