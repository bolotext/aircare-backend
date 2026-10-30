"""Single source of the automailer's reporting window: the preceding
Asia/Kolkata calendar day, keyed on call start time, start-inclusive and
end-exclusive. Both the scheduler and the manual `/run` trigger call
`previous_ist_calendar_day()` so the two code paths can never disagree about
what "yesterday" means.
"""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def previous_ist_calendar_day(reference: date | None = None) -> tuple[datetime, datetime]:
    """Returns (window_start, window_end) as UTC-aware instants bounding the
    IST calendar day before `reference` (default: today in IST) —
    start-inclusive, end-exclusive, i.e. [window_start, window_end).
    """

    today_ist = reference or datetime.now(IST).date()
    yesterday_ist = today_ist - timedelta(days=1)
    start_ist = datetime.combine(yesterday_ist, datetime.min.time(), tzinfo=IST)
    end_ist = datetime.combine(today_ist, datetime.min.time(), tzinfo=IST)
    return start_ist.astimezone(timezone.utc), end_ist.astimezone(timezone.utc)


def run_key_date_component(window_start: datetime) -> str:
    """The calendar-day label used inside a scheduled run_key — the IST date
    the window covers (i.e. the date of window_start converted to IST), not
    the UTC date, since window_start is already midnight IST expressed in UTC.
    """

    return window_start.astimezone(IST).date().isoformat()
