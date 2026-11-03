"""Runs scam.py's analytics on a dataset straight from MongoDB and stores each result in `auto_scam_results`.

No files are written. Conversations that already have a successful result (matched by customerCRTId) are
skipped unless the run is forced. Runs live in this process (asyncio tasks + a thread pool for the blocking
OpenAI calls); cancellation goes through a DB flag so it works even when the cancel request lands on a
different worker process.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from beanie import PydanticObjectId
from bson import ObjectId

from reports_service.auto_scam.model import AutoScamResult, AutoScamRun, utcnow
from reports_service.auto_scam.rules import apply_rules_traced
from reports_service.auto_scam.records import record_filter, record_key, records_collection

logger = logging.getLogger(__name__)

INTERNAL_FIELDS = ("_id", "project_id", "aircare_project_id", "source_id", "uploaded_at", "call_date")
SCAM_DIR = Path(__file__).resolve().parent / "scam"  # scam.py + the bundled analytics_prompt.yaml
STALE_AFTER = timedelta(seconds=90)
LOG_TAIL_CHARS = 20_000
PROGRESS_LOG_EVERY = 25

_tasks: set[asyncio.Task] = set()
# One run at a time per project: total OpenAI concurrency is then exactly that run's "Parallel workers" setting.
_project_locks: dict[str, asyncio.Lock] = {}


def _make_client():
    from reports_service.auto_scam.scam import scam as scam_mod

    return scam_mod.make_client()


def _analyze(conversation: dict, client, prompt_text: str, max_retries: int) -> dict:
    """Blocking (runs in a worker thread): the analytics dict for one conversation, or {"error": ...}."""
    from reports_service.auto_scam.scam import scam as scam_mod

    try:
        return scam_mod.analyze_conversation(conversation, client, prompt_text=prompt_text, max_retries=max_retries, normalize=False)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def submit(run_id: str) -> None:
    task = asyncio.create_task(_guarded(run_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def fail_stale_runs() -> None:
    """On startup: runs whose owner stopped heart-beating (process died) can never finish."""
    cutoff = utcnow() - STALE_AFTER
    async for run in AutoScamRun.find({"status": {"$in": ["queued", "running"]}}):
        beat = run.heartbeat_at or run.created_at
        if beat.tzinfo is None:
            beat = beat.replace(tzinfo=cutoff.tzinfo)
        if beat < cutoff:
            run.status, run.error, run.finished_at = "failed", "server stopped while this run was active", utcnow()
            await run.save()


async def _set(run_id: str, **fields) -> None:
    """Targeted update, so a stale in-memory run can never overwrite a cancel flag set by another request."""
    await AutoScamRun.find_one({"_id": PydanticObjectId(run_id)}).update({"$set": fields})


async def _guarded(run_id: str) -> None:
    try:
        run = await AutoScamRun.get(PydanticObjectId(run_id))
        lock = _project_locks.setdefault(str(run.aircare_project_id), asyncio.Lock()) if run else asyncio.Lock()
        async with lock:
            await _execute(run_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("auto_scam run %s crashed", run_id)
        await _set(run_id, status="failed", error=str(exc), finished_at=utcnow())


async def _analysed_keys(run: AutoScamRun) -> set[str]:
    """customerCRTIds that already have a successful result for this file."""
    cursor = AutoScamResult.get_pymongo_collection().find(
        {"aircare_project_id": run.aircare_project_id, "file_name": run.file_name,
         "error": None, "fields": {"$ne": []}},
        {"customer_crt_id": 1},
    )
    return {d["customer_crt_id"] async for d in cursor}


async def _store_result(run: AutoScamRun, key: str, doc: dict, analytics: dict, llm: dict | None = None, trace: list[dict] | None = None) -> bool:
    """Upsert the one result row for this customerCRTId. Returns True on success, False on a failed analysis."""
    error = analytics.get("error")
    # the analytics JSON as returned (after the rules): every top-level key with its value
    fields = [] if error else [{"field": k, "updated": v} for k, v in analytics.items()]
    ok = bool(fields)
    row = {
        "run_id": str(run.id), "record_id": str(doc["_id"]), "fields": fields,
        # the model's own answer before the rules, and what the rules changed (and why)
        "llm_fields": [{"field": k, "value": v} for k, v in (llm or {}).items()] if ok else [], "rule_trace": (trace or []) if ok else [],
        "error": None if ok else (error or "no analytics returned"), "created_at": utcnow(),
    }
    flt = {"aircare_project_id": run.aircare_project_id, "file_name": run.file_name, "customer_crt_id": key}
    col = AutoScamResult.get_pymongo_collection()
    # a failed attempt never overwrites an earlier successful result
    await col.update_one(flt, {"$set": row} if ok else {"$setOnInsert": row}, upsert=True)
    return ok


async def _execute(run_id: str) -> None:
    run = await AutoScamRun.get(PydanticObjectId(run_id))
    if run is None or run.cancel_requested:
        if run:
            await _set(run_id, status="cancelled", finished_at=utcnow())
        return

    client = _make_client()  # raises if the Azure OpenAI credentials are missing -> run marked failed
    done = set() if run.force else await _analysed_keys(run)
    todo: list[tuple[str, dict]] = []
    seen: set[str] = set()
    skipped = 0
    flt = record_filter(run.aircare_project_id, run.file_name, run.filters)
    if run.record_ids:  # explicit selection (ids outside this file never match)
        flt["_id"] = {"$in": [ObjectId(i) for i in run.record_ids]}
    cursor = records_collection().find(flt)
    async for doc in cursor:
        key = record_key(doc)
        if key in seen:  # duplicate customerCRTId within the file: analysed once
            continue
        seen.add(key)
        if key in done:
            skipped += 1
        else:
            todo.append((key, doc))

    scope = f"{len(run.record_ids)} selected" if run.record_ids else ("matching the filters" if run.filters else "whole file")
    log: list[str] = [f"scope: {scope}", f"{len(todo)} to analyse, {skipped} already analysed (skipped)"
                      f"{' [re-analyse all]' if run.force else ''}; {run.config.workers} workers"]
    await _set(run_id, status="running", started_at=utcnow(), total=len(todo), skipped=skipped,
               heartbeat_at=utcnow(), log="\n".join(log))

    cancelled = asyncio.Event()
    counts = {"ok": 0, "failed": 0}

    def _log_text() -> str:
        return "\n".join(log)[-LOG_TAIL_CHARS:]

    async def watcher() -> None:  # heartbeat + cancel flag + log, every 2s
        while True:
            await asyncio.sleep(2)
            fresh = await AutoScamRun.get(PydanticObjectId(run_id))
            if fresh and fresh.cancel_requested:
                cancelled.set()
            await _set(run_id, heartbeat_at=utcnow(), processed=counts["ok"] + counts["failed"],
                       failed=counts["failed"], log=_log_text())

    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=run.config.workers)
    sem = asyncio.Semaphore(run.config.workers)

    async def one(key: str, doc: dict) -> None:
        async with sem:
            if cancelled.is_set():
                return
            conversation = {k: v for k, v in doc.items() if k not in INTERNAL_FIELDS}
            analytics = await loop.run_in_executor(executor, _analyze, conversation, client, run.prompt_text, run.config.max_retries)
            llm, trace = dict(analytics), []
            if not analytics.get("error"):
                analytics, trace = apply_rules_traced(run.config.rules, conversation, analytics)  # dynamic, UI-configured logic
            if await _store_result(run, key, doc, analytics, llm, trace):
                counts["ok"] += 1
            else:
                counts["failed"] += 1
                log.append(f"{key}: failed - {analytics.get('error')}")
            n = counts["ok"] + counts["failed"]
            if n % PROGRESS_LOG_EVERY == 0 or n == len(todo):
                log.append(f"progress: {n}/{len(todo)}")

    watch = asyncio.create_task(watcher())
    try:
        await asyncio.gather(*(one(k, d) for k, d in todo))
    finally:
        watch.cancel()
        executor.shutdown(wait=False, cancel_futures=True)

    fresh = await AutoScamRun.get(PydanticObjectId(run_id))
    if cancelled.is_set() or (fresh and fresh.cancel_requested):
        status = "cancelled"
    else:
        status = "succeeded" if counts["failed"] == 0 else "completed_with_errors"
    if not todo and not cancelled.is_set():
        log.append("Nothing to do: every conversation already has analytics. Tick 're-analyse' to run them again.")
    log.append(f"finished: {counts['ok']} analysed, {counts['failed']} failed, {skipped} skipped")
    await _set(run_id, status=status, processed=counts["ok"] + counts["failed"], failed=counts["failed"],
               finished_at=utcnow(), log=_log_text())
