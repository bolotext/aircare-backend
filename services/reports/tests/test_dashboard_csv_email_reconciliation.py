import csv
import io
from datetime import datetime, timezone

import pytest
from beanie import PydanticObjectId

from reports_service.calls.repository import CallFilter, list_all
from reports_service.mailer.render import render_report_html
from reports_service.metrics.csv_export import build_csv
from reports_service.metrics.formatting import format_pct, format_seconds
from reports_service.metrics.service import compute_metrics
from tests.factories import make_call

pytestmark = pytest.mark.asyncio

PROJECT = PydanticObjectId()
EMPTY_PROJECT = PydanticObjectId()
ORG = PydanticObjectId()
NOW = datetime.now(timezone.utc)


def _csv_summary(csv_bytes: bytes) -> dict:
    rows = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8"))))
    return {row[0]: row[1] for row in rows if len(row) == 2}


async def test_dashboard_csv_and_email_agree_on_one_fixed_dataset(app):
    # Answered + complete.
    await make_call(
        PROJECT, ORG, conversation_id="c1", disposition="RESOLVED", language="hi-IN", connected_seconds=120.0
    )
    # Not answered at all.
    await make_call(
        PROJECT,
        ORG,
        conversation_id="c2",
        disposition="ABANDONED",
        language="en-IN",
        call_status="NO_ANSWER",
        call_connected_time=None,
        call_end_time=None,
        connected_seconds=None,
    )
    # Incomplete: connected but never ended.
    await make_call(
        PROJECT,
        ORG,
        conversation_id="c3",
        disposition="NEUTRAL",
        language="kn-IN",
        call_end_time=None,
        connected_seconds=None,
    )

    call_filter = CallFilter(project_ids=[str(PROJECT)])
    metrics = await compute_metrics(call_filter)
    calls = await list_all(call_filter)
    csv_bytes = build_csv(calls, metrics)
    html = render_report_html(scope_label="Project X", window_start=NOW, window_end=NOW, metrics=metrics)

    summary = _csv_summary(csv_bytes)
    assert summary["Total calls"] == str(metrics.total_calls) == "3"
    assert summary["Answered calls"] == str(metrics.answered_calls) == "2"
    assert summary["Incomplete calls"] == str(metrics.incomplete_calls_count) == "1"
    assert summary["Language: hi-IN"] == "1"
    assert summary["Disposition: RESOLVED"] == "1"

    # The HTML must show the exact same formatted strings the CSV/dashboard
    # derive from `metrics` — not a separately-computed number.
    assert format_pct(metrics.answer_rate_pct) in html
    assert format_seconds(metrics.avg_connected_seconds) in html
    assert "hi-IN" in html and "kn-IN" in html
    assert "RESOLVED" in html


async def test_empty_scope_reconciles_to_na_everywhere(app):
    call_filter = CallFilter(project_ids=[str(EMPTY_PROJECT)])
    metrics = await compute_metrics(call_filter)
    calls = await list_all(call_filter)
    csv_bytes = build_csv(calls, metrics)
    html = render_report_html(scope_label="Empty project", window_start=NOW, window_end=NOW, metrics=metrics)

    summary = _csv_summary(csv_bytes)
    assert summary["Total calls"] == "0"
    assert summary["Answer rate"] == "N/A"
    assert summary["Avg connected seconds"] == "N/A"
    assert summary["Scenario measure"] == "N/A"
    assert "N/A" in html  # answer rate / avg connected seconds render as N/A, never 0 or blank
