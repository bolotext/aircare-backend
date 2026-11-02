"""Chunked upload of big conversation files (hundreds of MB) with progress.

The browser never holds the file in memory or builds one giant request:
  1. POST /auto-scam/uploads            declare name + size (name conflicts fail here, before any data is sent)
  2. PUT  /auto-scam/uploads/{id}/chunk?offset=N   raw bytes, 8 MB at a time (retryable: same offset overwrites)
  3. POST /auto-scam/uploads/{id}/finish           the server parses the file block by block into MongoDB
  4. GET  /auto-scam/uploads/{id}                  progress: bytes received, then bytes processed + conversations stored
The new conversations are inserted under a staging project_id and only swapped in once the whole file parsed, so a
bad file never damages the existing data. Accepts a JSON array, JSON Lines, or a single object (Mongo extended JSON ok).
"""

import asyncio
import codecs
import json
import logging
import os
import tempfile
from datetime import timedelta
from pathlib import Path

from beanie import PydanticObjectId
from bson import json_util
from common.errors import BadRequestError, ConflictError, NotFoundError
from common.responses import success_response
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from reports_service.auto_scam.model import (
    AutoScamDataset, AutoScamConfig, AutoScamResult, AutoScamRun, AutoScamUpload, utcnow,
)
from reports_service.auto_scam.records import derive_call_date, records_collection
from reports_service.core.deps import ensure_project_access, require_permission
from reports_service.core.users_client import IdentityContext

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auto-scam/uploads", tags=["auto-scam"])

UPLOAD_DIR = Path(tempfile.gettempdir()) / "auto_scam_uploads"
CHUNK_SIZE = 8 * 1024 * 1024
MAX_UPLOAD_BYTES = 2 * 1024**3
MAX_OBJECT_CHARS = 64 * 1024 * 1024  # one conversation larger than this is rejected as malformed
INSERT_BATCH = 1000
RECEIVING_STALE = timedelta(hours=6)
PROCESSING_STALE = timedelta(seconds=90)
ACTIVE_RUN = ("queued", "running")

_tasks: set[asyncio.Task] = set()


class StartBody(BaseModel):
    project_id: str
    file_name: str = Field(min_length=1, max_length=200)
    size: int = Field(gt=0)
    overwrite: bool = False


class _Parser:
    """Incremental parser: feed bytes, get complete conversation objects back. Handles chunk boundaries that fall
    inside an object or inside a multi-byte UTF-8 character."""

    def __init__(self) -> None:
        self.decoder = json.JSONDecoder(object_hook=json_util.object_hook)
        self.text = codecs.getincrementaldecoder("utf-8-sig")()
        self.buf = ""
        self.mode: str | None = None  # "array" | "lines"
        self.closed = False

    def feed(self, data: bytes, final: bool) -> list[dict]:
        try:
            self.buf += self.text.decode(data, final)
        except UnicodeDecodeError as exc:
            raise ValueError(f"The file is not valid UTF-8 text ({exc.reason})") from exc
        buf, n, i, out = self.buf, len(self.buf), 0, []
        while True:
            while i < n and buf[i] in " \t\r\n,":
                i += 1
            if i >= n:
                break
            c = buf[i]
            if self.mode is None:
                self.mode = "array" if c == "[" else "lines"
                if c == "[":
                    i += 1
                    continue
            if c == "]" and self.mode == "array" and not self.closed:
                self.closed = True
                i += 1
                continue
            if self.closed:
                raise ValueError("Unexpected data after the closing ']'")
            if c != "{":
                raise ValueError("Expected conversation objects ({...}) in the file")
            try:
                obj, end = self.decoder.raw_decode(buf, i)
            except json.JSONDecodeError as exc:
                if final:
                    raise ValueError(f"Invalid JSON: {exc.msg} (near character {exc.pos})") from exc
                if n - i > MAX_OBJECT_CHARS:
                    raise ValueError("A single conversation is larger than 64 MB, or the JSON is malformed") from exc
                break  # object continues in the next block
            out.append(obj)
            i = end
        self.buf = buf[i:]
        if final and self.mode == "array" and not self.closed:
            raise ValueError("The file ended before the closing ']' (was the upload cut short?)")
        return out


def _prepare(objs: list[dict], pid: PydanticObjectId, staging: str, ts) -> list[dict]:
    docs = []
    for rec in objs:
        rec = dict(rec)
        source_id = rec.pop("_id", None)
        for reserved in ("project_id", "aircare_project_id", "call_date", "uploaded_at"):
            rec.pop(reserved, None)
        rec.update(project_id=staging, aircare_project_id=pid, uploaded_at=ts, call_date=derive_call_date(rec))
        if source_id is not None:
            rec["source_id"] = str(source_id)
        docs.append(rec)
    return docs


def _out(u: AutoScamUpload) -> dict:
    return {
        "id": str(u.id), "file_name": u.file_name, "status": u.status, "size": u.size, "received": u.received,
        "processed_bytes": u.processed_bytes, "records": u.records, "error": u.error,
    }


def _remove_file(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


async def _get(identity: IdentityContext, upload_id: str) -> AutoScamUpload:
    try:
        up = await AutoScamUpload.get(PydanticObjectId(upload_id))
    except Exception:
        up = None
    if up is None:
        raise NotFoundError("Upload not found")
    ensure_project_access(identity, str(up.aircare_project_id))
    return up


def _staging(up: AutoScamUpload) -> str:
    return f"__upload__{up.id}"


async def _discard(up: AutoScamUpload) -> None:
    await records_collection().delete_many({"aircare_project_id": up.aircare_project_id, "project_id": _staging(up)})
    await asyncio.to_thread(_remove_file, up.path)


async def sweep_uploads() -> None:
    """Startup: uploads whose owner is gone can never finish; drop their temp files and half-inserted data."""
    now = utcnow()
    async for up in AutoScamUpload.find({"status": {"$in": ["receiving", "processing"]}}):
        beat = up.heartbeat_at if up.heartbeat_at.tzinfo else up.heartbeat_at.replace(tzinfo=now.tzinfo)
        limit = RECEIVING_STALE if up.status == "receiving" else PROCESSING_STALE
        if now - beat > limit:
            await _discard(up)
            up.status, up.error, up.finished_at = "failed", "upload was abandoned or the server restarted", now
            await up.save()


# ------------------------------------------------------------------ endpoints
@router.post("", summary="Start a chunked upload (checks the file name before any data is sent)")
async def start_upload(body: StartBody, identity: IdentityContext = Depends(require_permission("auto_scam.manage"))) -> dict:
    from reports_service.auto_scam.router import FILE_NAME_RE, _pid  # shared validation

    ensure_project_access(identity, body.project_id)
    pid = _pid(body.project_id)
    name = body.file_name.strip()
    if not FILE_NAME_RE.match(name):
        raise BadRequestError("File name may only contain letters, digits, spaces, '.', '-' and '_'", {"field": "file_name"})
    if body.size > MAX_UPLOAD_BYTES:
        raise BadRequestError(f"File is larger than the {MAX_UPLOAD_BYTES // 1024**3} GB limit")
    existing = await AutoScamDataset.find_one({"aircare_project_id": pid, "file_name": name})
    if existing and not body.overwrite:
        raise ConflictError(f"'{name}' already exists. Enable overwrite to replace its data.")
    if existing and await AutoScamRun.find_one({"aircare_project_id": pid, "file_name": name, "status": {"$in": list(ACTIVE_RUN)}}):
        raise ConflictError("A run is active for this file; cancel it before replacing the data.")
    if await AutoScamUpload.find_one({"aircare_project_id": pid, "file_name": name, "status": {"$in": ["receiving", "processing"]}}):
        raise ConflictError(f"'{name}' is already being uploaded.")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    up = AutoScamUpload(aircare_project_id=pid, file_name=name, overwrite=body.overwrite, size=body.size, path="", created_by=identity.user_id)
    await up.insert()
    up.path = str(UPLOAD_DIR / f"{up.id}.part")
    await up.save()
    await asyncio.to_thread(lambda: open(up.path, "wb").close())
    return success_response(code=201, message="Upload started", data={"upload_id": str(up.id), "chunk_size": CHUNK_SIZE})


def _write_at(path: str, offset: int, data: bytes) -> None:
    with open(path, "r+b") as f:
        f.seek(offset)
        f.write(data)


@router.put("/{upload_id}/chunk", summary="Send one chunk (raw bytes) at a byte offset; resending an offset is safe")
async def upload_chunk(
    upload_id: str,
    request: Request,
    offset: int = Query(..., ge=0),
    identity: IdentityContext = Depends(require_permission("auto_scam.manage")),
) -> dict:
    up = await _get(identity, upload_id)
    if up.status != "receiving":
        raise ConflictError(f"Upload is {up.status}, not receiving data")
    data = await request.body()
    if not data or len(data) > CHUNK_SIZE * 2:
        raise BadRequestError("Chunk must be 1 byte to 16 MB")
    if offset > up.received:
        raise ConflictError(f"Chunk starts at {offset} but only {up.received} bytes were received so far", {"received": up.received})
    if offset + len(data) > up.size:
        raise BadRequestError("Chunk goes past the declared file size")
    await asyncio.to_thread(_write_at, up.path, offset, data)
    received = max(up.received, offset + len(data))
    await AutoScamUpload.find_one({"_id": up.id}).update({"$set": {"received": received, "heartbeat_at": utcnow()}})
    return success_response(data={"received": received})


@router.post("/{upload_id}/finish", summary="All chunks sent: parse the file into the dataset (poll GET for progress)")
async def finish_upload(upload_id: str, identity: IdentityContext = Depends(require_permission("auto_scam.manage"))) -> dict:
    up = await _get(identity, upload_id)
    if up.status != "receiving":
        raise ConflictError(f"Upload is already {up.status}")
    if up.received != up.size:
        raise ConflictError(f"Only {up.received} of {up.size} bytes were received", {"received": up.received})
    up.status, up.heartbeat_at = "processing", utcnow()
    await up.save()
    task = asyncio.create_task(_process(str(up.id)))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return success_response(code=202, message="Processing", data=_out(up))


@router.get("/{upload_id}", summary="Upload progress")
async def upload_status(upload_id: str, identity: IdentityContext = Depends(require_permission("auto_scam.manage"))) -> dict:
    return success_response(data=_out(await _get(identity, upload_id)))


@router.delete("/{upload_id}", summary="Cancel an upload and discard what was received")
async def cancel_upload(upload_id: str, identity: IdentityContext = Depends(require_permission("auto_scam.manage"))) -> dict:
    up = await _get(identity, upload_id)
    if up.status in ("done", "failed", "cancelled"):
        return success_response(message="Nothing to cancel", data=_out(up))
    up.status, up.finished_at = "cancelled", utcnow()  # a processing job notices this and stops
    await up.save()
    await _discard(up)
    return success_response(message="Cancelled", data=_out(up))


# ------------------------------------------------------------------ processing
async def _set(upload_id: str, **fields) -> None:
    await AutoScamUpload.find_one({"_id": PydanticObjectId(upload_id)}).update({"$set": fields})


async def _process(upload_id: str) -> None:
    up = await AutoScamUpload.get(PydanticObjectId(upload_id))
    if up is None:
        return
    pid, name, staging = up.aircare_project_id, up.file_name, _staging(up)
    parser, ts, total, read = _Parser(), utcnow(), 0, 0
    try:
        with open(up.path, "rb") as f:
            while True:
                block = await asyncio.to_thread(f.read, CHUNK_SIZE)
                final = not block
                objs = await asyncio.to_thread(parser.feed, block, final)
                docs = await asyncio.to_thread(_prepare, objs, pid, staging, ts)
                for i in range(0, len(docs), INSERT_BATCH):
                    await records_collection().insert_many(docs[i : i + INSERT_BATCH])
                total += len(docs)
                read += len(block)
                fresh = await AutoScamUpload.get(up.id)
                if fresh is None or fresh.status == "cancelled":
                    await _discard(up)
                    return
                await _set(upload_id, processed_bytes=read, records=total, heartbeat_at=utcnow())
                if final:
                    break
        if total == 0:
            raise ValueError("No conversations found in the file")

        existing = await AutoScamDataset.find_one({"aircare_project_id": pid, "file_name": name})
        if existing and not up.overwrite:
            raise ValueError(f"'{name}' was created while this file was uploading; upload again with overwrite")
        if existing:
            if await AutoScamRun.find_one({"aircare_project_id": pid, "file_name": name, "status": {"$in": list(ACTIVE_RUN)}}):
                raise ValueError("A run started on this file while it was uploading; cancel it and upload again")
            await records_collection().delete_many({"aircare_project_id": pid, "project_id": name})
            await AutoScamResult.find({"aircare_project_id": pid, "file_name": name}).delete()
        await records_collection().update_many({"aircare_project_id": pid, "project_id": staging}, {"$set": {"project_id": name}})
        if existing:
            existing.updated_at = utcnow()
            await existing.save()
        else:
            await AutoScamDataset(aircare_project_id=pid, file_name=name, config=_default_config(), created_by=up.created_by).insert()
        await _set(upload_id, status="done", processed_bytes=up.size, records=total, finished_at=utcnow())
    except Exception as exc:  # noqa: BLE001
        logger.warning("auto_scam upload %s failed: %s", upload_id, exc)
        await _discard(up)  # never leave half-inserted data behind; the previous dataset (if any) is untouched
        await _set(upload_id, status="failed", error=str(exc), finished_at=utcnow())  # only after cleanup, so "failed" means clean
    else:
        await asyncio.to_thread(_remove_file, up.path)


def _default_config() -> AutoScamConfig:
    from reports_service.auto_scam.router import _bundled_prompt

    return AutoScamConfig(prompt=_bundled_prompt())
