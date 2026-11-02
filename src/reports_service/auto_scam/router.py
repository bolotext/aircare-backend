"""Auto Scam API: upload conversation JSON, edit it, configure and run scam.py, review previous vs updated values.

Scoped by Aircare `project_id` (every route re-checks `ensure_project_access`); the uploaded file name is the
dataset key (stored as `project_id` on each conversation document in the `auto_scam` collection).
Conversations are call transcripts (PII): they are never logged, and running sends them to Azure OpenAI.
"""

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from beanie import PydanticObjectId
from bson import ObjectId, json_util
from common.errors import BadRequestError, ConflictError, NotFoundError, UnprocessableEntityError
from common.responses import success_response
from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel, Field

from reports_service.auto_scam import runner
from reports_service.auto_scam.model import AutoScamConfig, AutoScamDataset, AutoScamResult, AutoScamRun, utcnow
from reports_service.auto_scam.rules import Rule, apply_rules, default_rules
from reports_service.auto_scam.records import KEY_PROJECTION, record_filter, record_key, records_collection
from reports_service.core.deps import ensure_project_access, require_permission
from reports_service.core.users_client import IdentityContext

router = APIRouter(prefix="/auto-scam", tags=["auto-scam"])

ACTIVE = ("queued", "running")
RESERVED = ("_id", "id", "project_id", "aircare_project_id", "source_id", "uploaded_at")
FILE_NAME_RE = re.compile(r"^[\w][\w .\-]{0,198}$")


def _parse_upload(raw: str):
    """A JSON array (or a single object), or JSON Lines (one conversation per line, as `mongoexport` writes it).
    Mongo extended JSON ($oid, $date, ...) is understood."""
    try:
        return json_util.loads(raw)
    except ValueError as exc:
        lines = [ln for ln in raw.splitlines() if ln.strip()]
        if len(lines) > 1:
            try:
                return [json_util.loads(ln) for ln in lines]
            except ValueError:
                pass
        raise BadRequestError(f"Invalid JSON: {exc}")


class UploadBody(BaseModel):
    project_id: str
    file_name: str = Field(min_length=1, max_length=200)
    raw_json: str = Field(min_length=2, description="Raw JSON: an array of conversation objects (Mongo extended JSON accepted).")
    overwrite: bool = False


class RunBody(BaseModel):
    project_id: str
    force: bool = Field(False, description="Re-analyse conversations that already have a result (matched by customerCRTId).")
    # Optional subset (needs exactly one file): selected conversations, or every conversation matching `q`.
    record_ids: list[str] = Field(default_factory=list, max_length=1000)
    all_matching: bool = False
    q: str = Field("", max_length=100)
    file_names: list[str] = Field(min_length=1, max_length=50)


def _pid(project_id: str) -> PydanticObjectId:
    try:
        return PydanticObjectId(project_id)
    except Exception:
        raise BadRequestError("project_id is not valid", {"field": "project_id"})


def _iso(v):
    return v.isoformat() if v else None


def _plain(doc: dict) -> dict:
    """Mongo doc -> JSON-safe dict (extended JSON for ObjectId/dates)."""
    out = json.loads(json_util.dumps(doc, json_options=json_util.RELAXED_JSON_OPTIONS))
    out["id"] = str(doc["_id"])
    out.pop("_id", None)
    out.pop("aircare_project_id", None)
    return out


def _run_out(r: AutoScamRun) -> dict:
    return {
        "id": str(r.id), "file_name": r.file_name, "status": r.status, "total": r.total, "processed": r.processed,
        "failed": r.failed, "skipped": r.skipped, "force": r.force, "selected": len(r.record_ids), "search": r.search, "error": r.error, "exit_code": r.exit_code, "cancel_requested": r.cancel_requested,
        "created_at": _iso(r.created_at), "started_at": _iso(r.started_at), "finished_at": _iso(r.finished_at),
    }


def _bundled_prompt() -> str:
    try:
        return (runner.SCAM_DIR / "analytics_prompt.yaml").read_text(encoding="utf-8")
    except OSError:
        return "role: |\n  Describe the analytics task here.\n"


async def _dataset(project_id: str, file_name: str) -> AutoScamDataset:
    ds = await AutoScamDataset.find_one({"aircare_project_id": _pid(project_id), "file_name": file_name})
    if ds is None:
        raise NotFoundError("Dataset not found")
    return ds


async def _record(identity: IdentityContext, record_id: str) -> dict:
    try:
        oid = ObjectId(record_id)
    except Exception:
        raise BadRequestError("record id is not valid", {"field": "record_id"})
    doc = await records_collection().find_one({"_id": oid})
    if doc is None:
        raise NotFoundError("Record not found")
    ensure_project_access(identity, str(doc["aircare_project_id"]))
    return doc


def _from_json_body(body: dict) -> dict:
    try:
        return json_util.loads(json.dumps(body))
    except (TypeError, ValueError):
        raise BadRequestError("Record is not valid JSON")


# ------------------------------------------------------------------ datasets
@router.get("/defaults", summary="The bundled default prompt (analytics_prompt.yaml) and the default rules")
async def defaults(identity: IdentityContext = Depends(require_permission("auto_scam.view"))) -> dict:
    return success_response(data={"prompt": _bundled_prompt(), "rules": [r.model_dump() for r in default_rules()]})


class RulesTestBody(BaseModel):
    rules: list[Rule] = Field(max_length=50)
    conversation: dict = Field(default_factory=dict, description="Conversation fields the rules can read, e.g. STAGE_CODE, setupTime.")
    analytics: dict = Field(default_factory=dict, description="What the model returned, e.g. INTEREST_CLASSIFICATION.")


@router.post("/rules/test", summary="Dry-run rules on a sample (nothing is stored)")
async def test_rules(body: RulesTestBody, identity: IdentityContext = Depends(require_permission("auto_scam.view"))) -> dict:
    output, fired = apply_rules(body.rules, body.conversation, body.analytics)
    return success_response(data={"analytics": output, "fired": fired})


FIXED_VALUE_FIELDS = ("STAGE_CODE", "DISPOSITION", "INTEREST_CLASSIFICATION")
FIELD_VALUE_SAMPLE = 500


@router.get("/field-values", summary="The values STAGE_CODE / DISPOSITION / INTEREST_CLASSIFICATION take, for dropdowns")
async def field_values(
    project_id: str = Query(...),
    identity: IdentityContext = Depends(require_permission("auto_scam.view")),
) -> dict:
    """Distinct values of the fixed-vocabulary fields, taken from the project's most recent 500 conversations."""
    ensure_project_access(identity, project_id)
    projection = {f: 1 for f in FIXED_VALUE_FIELDS}
    docs = await records_collection().find({"aircare_project_id": _pid(project_id)}, projection).sort("_id", -1).limit(FIELD_VALUE_SAMPLE).to_list(FIELD_VALUE_SAMPLE)
    values: dict[str, set[str]] = {f: set() for f in FIXED_VALUE_FIELDS}
    for d in docs:
        for f in FIXED_VALUE_FIELDS:
            v = d.get(f)
            if isinstance(v, str) and v.strip():
                values[f].add(v.strip())
    return success_response(data={"sampled": len(docs), "values": {f: sorted(v) for f, v in values.items()}})


@router.get("/datasets", summary="List a project's uploaded datasets with record counts and last run")
async def list_datasets(
    project_id: str = Query(...),
    identity: IdentityContext = Depends(require_permission("auto_scam.view")),
) -> dict:
    ensure_project_access(identity, project_id)
    pid = _pid(project_id)
    counts = {
        d["_id"]: d["n"]
        async for d in records_collection().aggregate(
            [{"$match": {"aircare_project_id": pid}}, {"$group": {"_id": "$project_id", "n": {"$sum": 1}}}]
        )
    }
    analysed = {
        d["_id"]: d["n"]
        async for d in AutoScamResult.get_pymongo_collection().aggregate([
            {"$match": {"aircare_project_id": pid, "error": None, "fields": {"$ne": []}}},
            {"$group": {"_id": "$file_name", "n": {"$sum": 1}}},
        ])
    }
    items = []
    for ds in await AutoScamDataset.find({"aircare_project_id": pid}).sort("-created_at").to_list():
        last = await AutoScamRun.find({"aircare_project_id": pid, "file_name": ds.file_name}).sort("-created_at").first_or_none()
        items.append({
            "file_name": ds.file_name, "records": counts.get(ds.file_name, 0), "analysed": analysed.get(ds.file_name, 0), "created_at": _iso(ds.created_at),
            "updated_at": _iso(ds.updated_at), "last_run": _run_out(last) if last else None,
        })
    return success_response(data=items)


@router.post("/datasets/upload", summary="Upload a raw JSON file of conversations")
async def upload(body: UploadBody, identity: IdentityContext = Depends(require_permission("auto_scam.manage"))) -> dict:
    ensure_project_access(identity, body.project_id)
    pid = _pid(body.project_id)
    name = body.file_name.strip()
    if not FILE_NAME_RE.match(name):
        raise BadRequestError("File name may only contain letters, digits, spaces, '.', '-' and '_'", {"field": "file_name"})
    data = await asyncio.to_thread(_parse_upload, body.raw_json)
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not data or not all(isinstance(x, dict) for x in data):
        raise BadRequestError("JSON must be an array of conversation objects")

    existing = await AutoScamDataset.find_one({"aircare_project_id": pid, "file_name": name})
    if existing and not body.overwrite:
        raise ConflictError(f"'{name}' already exists. Enable overwrite to replace its data.")
    if existing:
        if await AutoScamRun.find_one({"aircare_project_id": pid, "file_name": name, "status": {"$in": list(ACTIVE)}}):
            raise ConflictError("A run is active for this file; cancel it before replacing the data.")
        await records_collection().delete_many({"aircare_project_id": pid, "project_id": name})
        await AutoScamResult.find({"aircare_project_id": pid, "file_name": name}).delete()

    ts = utcnow()
    docs = []
    for rec in data:
        rec = dict(rec)
        source_id = rec.pop("_id", None)
        for reserved in ("project_id", "aircare_project_id"):
            rec.pop(reserved, None)
        rec.update(project_id=name, aircare_project_id=pid, uploaded_at=ts)
        if source_id is not None:
            rec["source_id"] = str(source_id)
        docs.append(rec)
    for i in range(0, len(docs), 1000):
        await records_collection().insert_many(docs[i : i + 1000])
    if existing:
        existing.updated_at = ts
        await existing.save()
    else:
        await AutoScamDataset(
            aircare_project_id=pid, file_name=name, config=AutoScamConfig(prompt=_bundled_prompt()), created_by=identity.user_id
        ).insert()
    return success_response(code=201, message="Uploaded", data={"file_name": name, "records": len(docs)})


async def _delete_dataset(ds: AutoScamDataset) -> int:
    pid, name = ds.aircare_project_id, ds.file_name
    deleted = (await records_collection().delete_many({"aircare_project_id": pid, "project_id": name})).deleted_count
    await AutoScamResult.find({"aircare_project_id": pid, "file_name": name}).delete()
    await AutoScamRun.find({"aircare_project_id": pid, "file_name": name}).delete()
    await ds.delete()
    return deleted


async def _has_active_run(ds: AutoScamDataset) -> bool:
    return bool(await AutoScamRun.find_one(
        {"aircare_project_id": ds.aircare_project_id, "file_name": ds.file_name, "status": {"$in": list(ACTIVE)}}
    ))


@router.delete("/datasets/{file_name}", summary="Delete a dataset with its records, results and runs")
async def delete_dataset(
    file_name: str,
    project_id: str = Query(...),
    identity: IdentityContext = Depends(require_permission("auto_scam.manage")),
) -> dict:
    ensure_project_access(identity, project_id)
    ds = await _dataset(project_id, file_name)
    if await _has_active_run(ds):
        raise ConflictError("Cancel the active run first.")
    return success_response(message="Deleted", data={"deleted_records": await _delete_dataset(ds)})


class BulkDatasetsBody(BaseModel):
    project_id: str
    file_names: list[str] = Field(min_length=1, max_length=200)


@router.post("/datasets/bulk-delete", summary="Delete several datasets (those with an active run are skipped)")
async def bulk_delete_datasets(
    body: BulkDatasetsBody, identity: IdentityContext = Depends(require_permission("auto_scam.manage"))
) -> dict:
    ensure_project_access(identity, body.project_id)
    pid = _pid(body.project_id)
    deleted, skipped = [], []
    for name in dict.fromkeys(body.file_names):
        ds = await AutoScamDataset.find_one({"aircare_project_id": pid, "file_name": name})
        if ds is None:
            skipped.append({"file_name": name, "reason": "not found"})
        elif await _has_active_run(ds):
            skipped.append({"file_name": name, "reason": "run in progress"})
        else:
            await _delete_dataset(ds)
            deleted.append(name)
    return success_response(message="Deleted", data={"deleted": deleted, "skipped": skipped})


@router.get("/datasets/{file_name}/config", summary="Get a dataset's analytics settings")
async def get_config(
    file_name: str,
    project_id: str = Query(...),
    identity: IdentityContext = Depends(require_permission("auto_scam.view")),
) -> dict:
    ensure_project_access(identity, project_id)
    cfg = (await _dataset(project_id, file_name)).config
    return success_response(data=cfg.model_dump())


@router.put("/datasets/{file_name}/config", summary="Update a dataset's analytics settings")
async def put_config(
    file_name: str,
    body: AutoScamConfig,
    project_id: str = Query(...),
    identity: IdentityContext = Depends(require_permission("auto_scam.manage")),
) -> dict:
    ensure_project_access(identity, project_id)
    ds = await _dataset(project_id, file_name)
    ds.config, ds.updated_at = body, utcnow()
    await ds.save()
    return success_response(data=body.model_dump())


# ------------------------------------------------------------------ records CRUD
def _record_filter(ds: AutoScamDataset, q: str = "") -> dict[str, Any]:
    return record_filter(ds.aircare_project_id, ds.file_name, q)


async def _drop_results(pid, file_name: str, keys: list[str]) -> None:
    if keys:
        await AutoScamResult.find({"aircare_project_id": pid, "file_name": file_name, "customer_crt_id": {"$in": keys}}).delete()


class BulkRecordsBody(BaseModel):
    project_id: str
    record_ids: list[str] = Field(default_factory=list, max_length=1000)
    all_matching: bool = Field(False, description="Delete every conversation matching `q` (all of them if blank), ignoring record_ids.")
    q: str = Field("", max_length=100)


@router.post("/datasets/{file_name}/records/bulk-delete", summary="Delete several conversations (selected ids, or everything matching a search)")
async def bulk_delete_records(
    file_name: str, body: BulkRecordsBody, identity: IdentityContext = Depends(require_permission("auto_scam.manage"))
) -> dict:
    ensure_project_access(identity, body.project_id)
    ds = await _dataset(body.project_id, file_name)
    if body.all_matching:
        flt = _record_filter(ds, body.q)
    else:
        if not body.record_ids:
            raise BadRequestError("Select at least one conversation")
        try:
            oids = [ObjectId(r) for r in body.record_ids]
        except Exception:
            raise BadRequestError("record id is not valid", {"field": "record_ids"})
        flt = {**_record_filter(ds), "_id": {"$in": oids}}  # scoped to this dataset: foreign ids are ignored
    col = records_collection()
    docs = await col.find(flt, KEY_PROJECTION).to_list(None)
    deleted = (await col.delete_many({"_id": {"$in": [d["_id"] for d in docs]}})).deleted_count if docs else 0
    await _drop_results(ds.aircare_project_id, file_name, [record_key(d) for d in docs])
    return success_response(message="Deleted", data={"deleted": deleted})
@router.get("/datasets/{file_name}/records", summary="List a dataset's conversations (with the latest run's result)")
async def list_records(
    file_name: str,
    project_id: str = Query(...),
    skip: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=200),
    q: str = Query("", max_length=100),
    identity: IdentityContext = Depends(require_permission("auto_scam.view")),
) -> dict:
    ensure_project_access(identity, project_id)
    ds = await _dataset(project_id, file_name)
    flt = _record_filter(ds, q)
    col = records_collection()
    total = await col.count_documents(flt)
    page = await col.find(flt).sort("_id", 1).skip(skip).limit(limit).to_list(limit)
    results = {
        r.customer_crt_id: r
        async for r in AutoScamResult.find({
            "aircare_project_id": ds.aircare_project_id, "file_name": file_name,
            "customer_crt_id": {"$in": [record_key(d) for d in page]},
        })
    }
    names = [c.field for c in ds.config.updated_columns]  # the updated fields the user chose in Settings
    items = []
    for d in page:
        r = results.get(record_key(d))
        items.append({
            "id": str(d["_id"]), "customerCRTId": d.get("customerCRTId"), "STAGE_CODE": d.get("STAGE_CODE"),
            "DISPOSITION": d.get("DISPOSITION"), "messages": len(d.get("conversation_log") or []),
            "values": {n: d.get(n) for n in names},  # what the conversation had before analytics
            "result": {
                "updated": {f["field"]: f["updated"] for f in r.fields if f["field"] in names},
                "changed": {f["field"]: f["changed"] for f in r.fields if f["field"] in names},
                "error": r.error,
            } if r else None,
        })
    return success_response(data={"total": total, "skip": skip, "limit": limit, "columns": [c.model_dump() for c in ds.config.updated_columns], "items": items})


@router.get("/records/{record_id}", summary="Get one conversation (full document)")
async def get_record(record_id: str, identity: IdentityContext = Depends(require_permission("auto_scam.view"))) -> dict:
    return success_response(data=_plain(await _record(identity, record_id)))


@router.post("/datasets/{file_name}/records", summary="Add a conversation")
async def create_record(
    file_name: str,
    body: dict = Body(...),
    project_id: str = Query(...),
    identity: IdentityContext = Depends(require_permission("auto_scam.manage")),
) -> dict:
    ensure_project_access(identity, project_id)
    ds = await _dataset(project_id, file_name)
    rec = _from_json_body(body)
    for key in RESERVED:
        rec.pop(key, None)
    rec.update(project_id=file_name, aircare_project_id=ds.aircare_project_id, uploaded_at=utcnow())
    res = await records_collection().insert_one(rec)
    return success_response(code=201, message="Created", data={"id": str(res.inserted_id)})


@router.put("/records/{record_id}", summary="Replace a conversation's content")
async def update_record(
    record_id: str,
    body: dict = Body(...),
    identity: IdentityContext = Depends(require_permission("auto_scam.manage")),
) -> dict:
    existing = await _record(identity, record_id)
    rec = _from_json_body(body)
    for key in RESERVED:
        rec.pop(key, None)
    keep = {k: existing[k] for k in ("project_id", "aircare_project_id", "source_id", "uploaded_at") if k in existing}
    await records_collection().replace_one({"_id": existing["_id"]}, {**rec, **keep})
    # the content changed, so its analytics are stale: drop them and the next run re-analyses it
    await _drop_results(existing["aircare_project_id"], existing["project_id"], [record_key(existing)])
    return success_response(message="Updated", data={"id": record_id})


@router.delete("/records/{record_id}", summary="Delete a conversation")
async def delete_record(record_id: str, identity: IdentityContext = Depends(require_permission("auto_scam.manage"))) -> dict:
    doc = await _record(identity, record_id)
    await records_collection().delete_one({"_id": doc["_id"]})
    await _drop_results(doc["aircare_project_id"], doc["project_id"], [record_key(doc)])
    return success_response(message="Deleted", data={"id": record_id})


# ------------------------------------------------------------------ runs
@router.post("/runs", summary="Run analytics (scam.py) on the selected datasets")
async def start_runs(body: RunBody, identity: IdentityContext = Depends(require_permission("auto_scam.run"))) -> dict:
    ensure_project_access(identity, body.project_id)
    pid = _pid(body.project_id)
    subset = bool(body.record_ids) or body.all_matching
    if subset and len(set(body.file_names)) != 1:
        raise BadRequestError("Running a selection needs exactly one file")
    try:
        for rid in body.record_ids:
            ObjectId(rid)
    except Exception:
        raise BadRequestError("record id is not valid", {"field": "record_ids"})
    started, skipped = [], []
    for name in dict.fromkeys(body.file_names):
        ds = await AutoScamDataset.find_one({"aircare_project_id": pid, "file_name": name})
        if ds is None:
            skipped.append({"file_name": name, "reason": "not found"})
        elif await AutoScamRun.find_one({"aircare_project_id": pid, "file_name": name, "status": {"$in": list(ACTIVE)}}):
            skipped.append({"file_name": name, "reason": "already running"})
        elif not await records_collection().count_documents({"aircare_project_id": pid, "project_id": name}, limit=1):
            skipped.append({"file_name": name, "reason": "no records"})
        else:
            run = await AutoScamRun(
                aircare_project_id=pid, file_name=name, config=ds.config, started_by=identity.user_id,
                prompt_text=ds.config.prompt, force=body.force, record_ids=body.record_ids,
                search=body.q if body.all_matching else None,
            ).insert()
            runner.submit(str(run.id))
            started.append({"run_id": str(run.id), "file_name": name})
    return success_response(code=202, message="Runs queued", data={"started": started, "skipped": skipped})


@router.get("/runs", summary="List runs for a project (optionally one dataset)")
async def list_runs(
    project_id: str = Query(...),
    file_name: str | None = Query(None),
    limit: int = Query(30, ge=1, le=100),
    identity: IdentityContext = Depends(require_permission("auto_scam.view")),
) -> dict:
    ensure_project_access(identity, project_id)
    flt: dict[str, Any] = {"aircare_project_id": _pid(project_id)}
    if file_name:
        flt["file_name"] = file_name
    runs = await AutoScamRun.find(flt).sort("-created_at").limit(limit).to_list()
    return success_response(data=[_run_out(r) for r in runs])


async def _get_run(identity: IdentityContext, run_id: str) -> AutoScamRun:
    try:
        run = await AutoScamRun.get(PydanticObjectId(run_id))
    except Exception:
        run = None
    if run is None:
        raise NotFoundError("Run not found")
    ensure_project_access(identity, str(run.aircare_project_id))
    return run


@router.get("/runs/{run_id}", summary="Run status with the tail of scam.py's output")
async def get_run(
    run_id: str,
    log_lines: int = Query(200, ge=1, le=2000),
    identity: IdentityContext = Depends(require_permission("auto_scam.view")),
) -> dict:
    run = await _get_run(identity, run_id)
    log = "".join(run.log.splitlines(keepends=True)[-log_lines:])
    return success_response(data={**_run_out(run), "log": log})


@router.post("/runs/{run_id}/cancel", summary="Cancel a queued or running run")
async def cancel_run(run_id: str, identity: IdentityContext = Depends(require_permission("auto_scam.run"))) -> dict:
    run = await _get_run(identity, run_id)
    if run.status not in ACTIVE:
        raise ConflictError("Run is not active")
    run.cancel_requested = True
    await run.save()
    return success_response(message="Cancel requested", data={"id": run_id})


@router.get("/datasets/{file_name}/results", summary="Per-conversation analytics: previous vs updated stage code and lead score")
async def dataset_results(
    file_name: str,
    project_id: str = Query(...),
    changed_only: bool = Query(False),
    identity: IdentityContext = Depends(require_permission("auto_scam.view")),
) -> dict:
    ensure_project_access(identity, project_id)
    ds = await _dataset(project_id, file_name)
    flt: dict[str, Any] = {"aircare_project_id": ds.aircare_project_id, "file_name": file_name}
    if changed_only:
        flt["changed"] = True
    rows = await AutoScamResult.find(flt).sort("+_id").to_list()
    items = [
        {"id": str(r.id), "run_id": r.run_id, "record_id": r.record_id, "customerCRTId": r.customer_crt_id,
         "disposition": r.disposition, "previous_stage_code": r.previous_stage_code,
         "updated_stage_code": r.updated_stage_code, "fields": r.fields, "changed": r.changed, "error": r.error}
        for r in rows
    ]
    return success_response(data={"columns": [c.model_dump() for c in ds.config.updated_columns], "items": items})
