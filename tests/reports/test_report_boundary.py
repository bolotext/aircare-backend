from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from beanie import PydanticObjectId

from reports_service.calls.repository import CallFilter
from reports_service.mailer.window import previous_ist_calendar_day
from reports_service.metrics.service import compute_metrics
from tests.reports.factories import make_call

PROJECT = PydanticObjectId()
ORG = PydanticObjectId()
IST = ZoneInfo("Asia/Kolkata")


def test_window_is_the_preceding_ist_calendar_day_start_inclusive_end_exclusive():
    reference = date(2026, 9, 20)
    start, end = previous_ist_calendar_day(reference)

    assert start.astimezone(IST) == datetime(2026, 9, 19, 0, 0, 0, tzinfo=IST)
    assert end.astimezone(IST) == datetime(2026, 9, 20, 0, 0, 0, tzinfo=IST)


async def test_boundary_calls_included_and_excluded_correctly(app):
    reference = date(2026, 9, 20)
    window_start, window_end = previous_ist_calendar_day(reference)

    await make_call(PROJECT, ORG, conversation_id="at-start", call_start_time=window_start)
    await make_call(
        PROJECT, ORG, conversation_id="just-before-end", call_start_time=window_end - timedelta(seconds=1)
    )
    # end-exclusive: a call starting exactly at window_end belongs to the NEXT day's report.
    await make_call(PROJECT, ORG, conversation_id="at-end", call_start_time=window_end)
    # just before window_start belongs to the PREVIOUS day's report.
    await make_call(
        PROJECT, ORG, conversation_id="just-before-start", call_start_time=window_start - timedelta(seconds=1)
    )

    call_filter = CallFilter(project_ids=[str(PROJECT)], date_from=window_start, date_to=window_end)
    metrics = await compute_metrics(call_filter)
    assert metrics.total_calls == 2
