"""The single reused metrics computation. `compute_metrics()` is called by
the dashboard route, the CSV export, and (Phase 3) the automailer — this is
what makes "dashboard, CSV and email must use identical metric definitions"
structurally true rather than something three separate implementations
happen to agree on.

Explicit, documented definitions (per the assignment brief's own instruction
to define these explicitly):

- Answered = call_status in {"ANSWERED", "COMPLETED"} AND call_connected_time
  is not None.
- Avg connected seconds = mean of connected_seconds over answered calls where
  connected_seconds is not None. A call with a connect time but no end time
  is "incomplete": counted in total_calls, excluded from this average, and
  surfaced separately via incomplete_calls_count so it is never silently
  dropped.
- Successful dispositions = the explicit SUCCESSFUL_DISPOSITIONS constant
  below; everything else (e.g. ABANDONED, NO_ANSWER, VOICEMAIL,
  TECHNICAL_FAILURE) is unsuccessful.
- Tool failures = sum of tool_failure_count across the filtered calls (a
  count of failed tool invocations, not of affected calls);
  calls_with_tool_failure is the secondary "how many calls had >=1 tool
  failure" figure.
- Unique call IDs = distinct conversation_id count, computed explicitly
  rather than assumed equal to total_calls (defensive against a future
  duplicate-write bug).
- Every ratio (answer_rate_pct, avg_connected_seconds, success_rate_pct) is
  None — never 0 or NaN — when its denominator is zero. Presentation layers
  render None as the literal string "N/A" via metrics.formatting.
"""

from collections.abc import Callable

from motor.motor_asyncio import AsyncIOMotorCollection

from reports_service.calls.model import Call
from reports_service.calls.repository import CallFilter, build_mongo_query, fetch_normalized_calls, matches_filter
from reports_service.metrics.schema import DashboardMetrics
from reports_service.scenarios.service import get_latest_scenario_measure

ANSWERED_STATUSES = {"ANSWERED", "COMPLETED"}
SUCCESSFUL_DISPOSITIONS = {"RESOLVED", "APPOINTMENT_BOOKED", "INFO_PROVIDED", "TRANSFERRED_SUCCESSFULLY"}


def _compute_metrics_from_calls(
    calls: list[Call], scenario_measure_pct: float | None, scenario_measure_available: bool
) -> DashboardMetrics:
    """The Python-side equivalent of the Mongo aggregation pipeline below,
    applied to an already-filtered, already-normalized list of Calls — used
    for the adapter path (see compute_metrics), whose source collection's
    own field names don't support pushing these aggregations down as a
    Mongo query. Must stay in sync with the metric definitions in this
    module's docstring — the same duplication tradeoff documented on
    calls.repository.matches_filter, for the same reason (a normalized
    source has no native fields left to aggregate on).
    """

    total_calls = len(calls)
    unique_call_ids = len({c.conversation_id for c in calls})

    answered = [c for c in calls if c.call_status in ANSWERED_STATUSES and c.call_connected_time is not None]
    answered_calls = len(answered)

    avg_pool = [c.connected_seconds for c in answered if c.connected_seconds is not None]
    avg_connected_seconds = (sum(avg_pool) / len(avg_pool)) if avg_pool else None

    incomplete_calls_count = sum(1 for c in calls if c.call_connected_time is not None and c.call_end_time is None)

    dispositions: dict[str, int] = {}
    for c in calls:
        if c.disposition is not None:
            dispositions[c.disposition] = dispositions.get(c.disposition, 0) + 1

    language_breakdown: dict[str, int] = {}
    for c in calls:
        if c.language is not None:
            language_breakdown[c.language] = language_breakdown.get(c.language, 0) + 1

    successful_disposition_count = sum(1 for c in calls if c.disposition in SUCCESSFUL_DISPOSITIONS)

    tool_failure_count = sum(c.tool_failure_count for c in calls)
    calls_with_tool_failure = sum(1 for c in calls if c.tool_failure_count > 0)

    answer_rate_pct = (answered_calls / total_calls * 100) if total_calls else None
    success_rate_pct = (successful_disposition_count / total_calls * 100) if total_calls else None

    return DashboardMetrics(
        total_calls=total_calls,
        unique_call_ids=unique_call_ids,
        answered_calls=answered_calls,
        answer_rate_pct=answer_rate_pct,
        avg_connected_seconds=avg_connected_seconds,
        incomplete_calls_count=incomplete_calls_count,
        dispositions=dispositions,
        language_breakdown=language_breakdown,
        successful_disposition_count=successful_disposition_count,
        success_rate_pct=success_rate_pct,
        tool_failure_count=tool_failure_count,
        calls_with_tool_failure=calls_with_tool_failure,
        scenario_measure_pct=scenario_measure_pct,
        scenario_measure_available=scenario_measure_available,
    )


async def compute_metrics(
    call_filter: CallFilter,
    collection: AsyncIOMotorCollection | None = None,
    adapter: Callable[..., dict] | None = None,
    organization_id: str | None = None,
) -> DashboardMetrics:
    """`collection` defaults to the fixed shared `calls` collection; the
    dashboard route passes in a project's own configured report collection
    instead (see core.deps.resolve_project_source) so CSV export and the
    automailer — which don't pass one — are unaffected. `adapter` (and the
    `organization_id` it needs) switches to computing every metric in Python
    from the normalized, filtered call list (see _compute_metrics_from_calls)
    for a project whose configured collection isn't already in this
    service's own Call schema.
    """

    collection = collection if collection is not None else Call.get_pymongo_collection()

    if adapter is not None:
        project_id = (call_filter.project_ids or [None])[0]
        all_calls = await fetch_normalized_calls(collection, adapter, project_id, organization_id or "")
        calls = [c for c in all_calls if matches_filter(c, call_filter)]
        scenario_measure_pct, scenario_measure_available = await get_latest_scenario_measure()
        return _compute_metrics_from_calls(calls, scenario_measure_pct, scenario_measure_available)

    query = build_mongo_query(call_filter)

    total_calls = await collection.count_documents(query)
    unique_call_ids = len(await collection.distinct("conversation_id", query))

    answered_query = {
        **query,
        "call_status": {"$in": list(ANSWERED_STATUSES)},
        "call_connected_time": {"$ne": None},
    }
    answered_calls = await collection.count_documents(answered_query)

    avg_query = {**answered_query, "connected_seconds": {"$ne": None}}
    avg_docs = await collection.aggregate(
        [{"$match": avg_query}, {"$group": {"_id": None, "avg": {"$avg": "$connected_seconds"}}}]
    ).to_list(1)
    avg_connected_seconds = avg_docs[0]["avg"] if avg_docs else None

    incomplete_query = {**query, "call_connected_time": {"$ne": None}, "call_end_time": None}
    incomplete_calls_count = await collection.count_documents(incomplete_query)

    disposition_docs = await collection.aggregate(
        [{"$match": query}, {"$group": {"_id": "$disposition", "count": {"$sum": 1}}}]
    ).to_list(None)
    dispositions = {doc["_id"]: doc["count"] for doc in disposition_docs if doc["_id"] is not None}

    language_docs = await collection.aggregate(
        [{"$match": query}, {"$group": {"_id": "$language", "count": {"$sum": 1}}}]
    ).to_list(None)
    language_breakdown = {doc["_id"]: doc["count"] for doc in language_docs if doc["_id"] is not None}

    successful_query = {**query, "disposition": {"$in": list(SUCCESSFUL_DISPOSITIONS)}}
    successful_disposition_count = await collection.count_documents(successful_query)

    tool_docs = await collection.aggregate(
        [
            {"$match": query},
            {
                "$group": {
                    "_id": None,
                    "total": {"$sum": "$tool_failure_count"},
                    "with_failure": {"$sum": {"$cond": [{"$gt": ["$tool_failure_count", 0]}, 1, 0]}},
                }
            },
        ]
    ).to_list(1)
    tool_failure_count = tool_docs[0]["total"] if tool_docs else 0
    calls_with_tool_failure = tool_docs[0]["with_failure"] if tool_docs else 0

    answer_rate_pct = (answered_calls / total_calls * 100) if total_calls else None
    success_rate_pct = (successful_disposition_count / total_calls * 100) if total_calls else None

    scenario_measure_pct, scenario_measure_available = await get_latest_scenario_measure()

    return DashboardMetrics(
        total_calls=total_calls,
        unique_call_ids=unique_call_ids,
        answered_calls=answered_calls,
        answer_rate_pct=answer_rate_pct,
        avg_connected_seconds=avg_connected_seconds,
        incomplete_calls_count=incomplete_calls_count,
        dispositions=dispositions,
        language_breakdown=language_breakdown,
        successful_disposition_count=successful_disposition_count,
        success_rate_pct=success_rate_pct,
        tool_failure_count=tool_failure_count,
        calls_with_tool_failure=calls_with_tool_failure,
        scenario_measure_pct=scenario_measure_pct,
        scenario_measure_available=scenario_measure_available,
    )
