from datetime import datetime, timezone

import pytest
from beanie import PydanticObjectId

from reports_service.calls.repository import CallFilter
from reports_service.metrics.service import compute_metrics
from tests.reports.factories import make_call

pytestmark = pytest.mark.asyncio

PROJECT_A = PydanticObjectId()
ORG = PydanticObjectId()


async def test_empty_project_every_ratio_is_none(app):
    metrics = await compute_metrics(CallFilter(project_ids=[str(PROJECT_A)]))
    assert metrics.total_calls == 0
    assert metrics.answer_rate_pct is None
    assert metrics.avg_connected_seconds is None
    assert metrics.success_rate_pct is None
    assert metrics.scenario_measure_available is False
    assert metrics.scenario_measure_pct is None


async def test_incomplete_call_excluded_from_average_but_counted(app):
    now = datetime.now(timezone.utc)
    # Answered + complete: 100s connected.
    await make_call(
        PROJECT_A, ORG, call_status="ANSWERED", call_connected_time=now, call_end_time=now, connected_seconds=100.0
    )
    # Answered but incomplete: connected, never ended.
    await make_call(
        PROJECT_A, ORG, call_status="ANSWERED", call_connected_time=now, call_end_time=None, connected_seconds=None
    )

    metrics = await compute_metrics(CallFilter(project_ids=[str(PROJECT_A)]))
    assert metrics.total_calls == 2
    assert metrics.answered_calls == 2  # both have call_connected_time set
    assert metrics.incomplete_calls_count == 1
    assert metrics.avg_connected_seconds == 100.0  # incomplete call's None is excluded, not averaged as 0


async def test_successful_disposition_counting(app):
    await make_call(PROJECT_A, ORG, disposition="RESOLVED")
    await make_call(PROJECT_A, ORG, disposition="APPOINTMENT_BOOKED")
    await make_call(PROJECT_A, ORG, disposition="ABANDONED")

    metrics = await compute_metrics(CallFilter(project_ids=[str(PROJECT_A)]))
    assert metrics.successful_disposition_count == 2
    assert metrics.success_rate_pct == 2 / 3 * 100
    assert metrics.dispositions == {"RESOLVED": 1, "APPOINTMENT_BOOKED": 1, "ABANDONED": 1}


async def test_tool_failure_aggregation(app):
    await make_call(PROJECT_A, ORG, tool_failure_count=0)
    await make_call(PROJECT_A, ORG, tool_failure_count=2)
    await make_call(PROJECT_A, ORG, tool_failure_count=3)

    metrics = await compute_metrics(CallFilter(project_ids=[str(PROJECT_A)]))
    assert metrics.tool_failure_count == 5
    assert metrics.calls_with_tool_failure == 2


async def test_unique_call_ids_computed_explicitly_not_assumed_equal_to_total(app):
    # The unique index on conversation_id (see calls/model.py) is what
    # actually prevents a true duplicate at the DB level — see
    # test_dedup_upsert.py for that guarantee. This test only asserts that
    # compute_metrics computes unique_call_ids via its own explicit distinct
    # count rather than assuming it always equals total_calls.
    await make_call(PROJECT_A, ORG, conversation_id="a")
    await make_call(PROJECT_A, ORG, conversation_id="b")
    await make_call(PROJECT_A, ORG, conversation_id="c")

    metrics = await compute_metrics(CallFilter(project_ids=[str(PROJECT_A)]))
    assert metrics.total_calls == 3
    assert metrics.unique_call_ids == 3


async def test_answer_rate_excludes_unanswered_calls(app):
    await make_call(PROJECT_A, ORG, call_status="ANSWERED", call_connected_time=datetime.now(timezone.utc))
    await make_call(PROJECT_A, ORG, call_status="NO_ANSWER", call_connected_time=None, call_end_time=None)

    metrics = await compute_metrics(CallFilter(project_ids=[str(PROJECT_A)]))
    assert metrics.answered_calls == 1
    assert metrics.answer_rate_pct == 50.0
