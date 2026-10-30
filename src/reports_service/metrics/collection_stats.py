"""Funnel / trend / hourly / duration aggregates for the collection-style
dashboard. Computed in Python over the already-filtered, already-normalized
`Call` list (`calls.repository.list_all`), so the native and schema-adapter
sources share one implementation and one set of definitions:

- Dialled = every call in the filter.
- Connected = same definition as `metrics.service` (ANSWERED/COMPLETED with a
  connect time).
- Engaged = connected AND connected_seconds >= `engaged_min_seconds`.
- Positive disposition = disposition in `SUCCESSFUL_DISPOSITIONS`.
- Hours and days are bucketed in UTC.

There is no per-account key on `Call` (phone numbers are excluded as PII), so
there is deliberately no "unique accounts" figure here.
"""

from datetime import timezone

from pydantic import BaseModel, Field

from reports_service.calls.model import Call
from reports_service.metrics.service import ANSWERED_STATUSES, SUCCESSFUL_DISPOSITIONS

# (label, inclusive lower bound, exclusive upper bound) in seconds.
DURATION_BUCKETS = [("0-15 sec", 0, 16), ("16-60 sec", 16, 61), ("60+ sec", 61, None)]


class FunnelStage(BaseModel):
    label: str
    count: int
    pct_of_dialled: float | None = Field(description="`null` when nothing was dialled.")


class TrendPoint(BaseModel):
    date: str = Field(description="UTC calendar day, `YYYY-MM-DD`.")
    attempts: int
    connected: int


class HourlyPoint(BaseModel):
    hour: int = Field(ge=0, le=23, description="UTC hour of day.")
    total: int
    connected: int


class DurationBucket(BaseModel):
    label: str
    count: int


class CollectionStats(BaseModel):
    engaged_min_seconds: int
    funnel: list[FunnelStage]
    trend: list[TrendPoint]
    hourly: list[HourlyPoint]
    dispositions: dict[str, int]
    statuses: dict[str, int]
    languages: dict[str, int]
    duration_buckets: list[DurationBucket]


def _is_connected(call: Call) -> bool:
    return call.call_status in ANSWERED_STATUSES and call.call_connected_time is not None


def _utc(value):
    return value.astimezone(timezone.utc) if value.tzinfo is not None else value


def compute_collection_stats(calls: list[Call], engaged_min_seconds: int) -> CollectionStats:
    total = len(calls)
    connected = [c for c in calls if _is_connected(c)]
    engaged = [c for c in connected if c.connected_seconds is not None and c.connected_seconds >= engaged_min_seconds]
    positive = [c for c in calls if c.disposition in SUCCESSFUL_DISPOSITIONS]

    def stage(label: str, count: int) -> FunnelStage:
        return FunnelStage(label=label, count=count, pct_of_dialled=(count / total * 100) if total else None)

    trend: dict[str, list[int]] = {}
    hourly: dict[int, list[int]] = {}
    dispositions: dict[str, int] = {}
    statuses: dict[str, int] = {}
    languages: dict[str, int] = {}
    for c in calls:
        start = _utc(c.call_start_time)
        is_conn = _is_connected(c)
        day = trend.setdefault(start.strftime("%Y-%m-%d"), [0, 0])
        day[0] += 1
        day[1] += is_conn
        hour = hourly.setdefault(start.hour, [0, 0])
        hour[0] += 1
        hour[1] += is_conn
        if c.disposition is not None:
            dispositions[c.disposition] = dispositions.get(c.disposition, 0) + 1
        if c.call_status is not None:
            statuses[c.call_status] = statuses.get(c.call_status, 0) + 1
        if c.language is not None:
            languages[c.language] = languages.get(c.language, 0) + 1

    buckets = []
    for label, low, high in DURATION_BUCKETS:
        count = sum(
            1
            for c in calls
            if (secs := c.connected_seconds or 0) >= low and (high is None or secs < high)
        )
        buckets.append(DurationBucket(label=label, count=count))

    return CollectionStats(
        engaged_min_seconds=engaged_min_seconds,
        funnel=[
            stage("Calls Dialled", total),
            stage("Calls Connected", len(connected)),
            stage("Calls Engaged", len(engaged)),
            stage("Positive Disposition", len(positive)),
        ],
        trend=[TrendPoint(date=d, attempts=v[0], connected=v[1]) for d, v in sorted(trend.items())],
        hourly=[HourlyPoint(hour=h, total=v[0], connected=v[1]) for h, v in sorted(hourly.items())],
        dispositions=dispositions,
        statuses=statuses,
        languages=languages,
        duration_buckets=buckets,
    )
