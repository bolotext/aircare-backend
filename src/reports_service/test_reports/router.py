"""Test Reports API: import a CSV/Excel QA sheet, browse its rows, edit cells, add/rename/delete columns.

Scoped by Aircare `project_id` (every route re-checks `ensure_project_access`). Rows can hold call
transcripts and sender ids: their values are never logged.
"""

import base64
import binascii
import re

from beanie import PydanticObjectId
from bson import ObjectId
from common.errors import BadRequestError, NotFoundError
from common.responses import success_response
from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel, Field

from reports_service.core.config import get_settings
from reports_service.core.deps import ensure_project_access, require_permission
from reports_service.core.users_client import IdentityContext
from reports_service.db.mongodb import get_client
from reports_service.test_reports import exporter, parser
from reports_service.test_reports.model import FILES_COLLECTION, ROWS_COLLECTION, Column, TestReport, utcnow

router = APIRouter(prefix="/test-reports", tags=["test-reports"])
MAX_FILE_BYTES = 15 * 1024 * 1024
MAX_CELL = 50000


def rows_coll():
    return get_client()[get_settings().mongo_db_name][ROWS_COLLECTION]


def files_coll():
    return get_client()[get_settings().mongo_db_name][FILES_COLLECTION]


def _oid(value: str, field: str = "id") -> ObjectId:
    try:
        return ObjectId(value)
    except Exception:
        raise BadRequestError(f"{field} is not valid", {"field": field})


def _report_out(r: TestReport) -> dict:
    return {
        "id": str(r.id), "name": r.name, "source_file": r.source_file, "sheet": r.sheet,
        "columns": [c.model_dump() for c in r.columns], "row_count": r.row_count,
        "created_by": r.created_by, "created_at": r.created_at.isoformat(), "updated_at": r.updated_at.isoformat(),
    }


def _row_out(d: dict) -> dict:
    return {"id": str(d["_id"]), "position": d.get("position", 0), "data": d.get("data", {})}


async def _report(identity: IdentityContext, report_id: str) -> TestReport:
    r = await TestReport.get(PydanticObjectId(_oid(report_id, "report_id")))
    if r is None:
        raise NotFoundError("Test report not found")
    ensure_project_access(identity, str(r.project_id))
    return r


def _clean_label(label: str) -> str:
    label = label.strip()
    if not label or len(label) > 100:
        raise BadRequestError("Column name must be 1-100 characters")
    return label


def _clean_data(data: dict, columns: list[Column]) -> dict:
    keys = {c.key for c in columns}
    out = {}
    for k, v in data.items():
        if k not in keys:
            raise BadRequestError(f"Unknown column '{k}'")
        out[k] = "" if v is None else str(v)[:MAX_CELL]
    return out


class SheetsBody(BaseModel):
    file_name: str = Field(min_length=1, max_length=200)
    content_b64: str = Field(min_length=4)


class CreateBody(SheetsBody):
    project_id: str
    name: str = Field(min_length=1, max_length=150)
    sheet: str | None = None


class RenameBody(BaseModel):
    name: str = Field(min_length=1, max_length=150)


class ColumnBody(BaseModel):
    label: str
    default: str = ""


class LabelBody(BaseModel):
    label: str


class RowBody(BaseModel):
    data: dict[str, str | int | float | None] = Field(default_factory=dict)


def _decode(b64: str) -> bytes:
    try:
        content = base64.b64decode(b64, validate=False)
    except (binascii.Error, ValueError):
        raise BadRequestError("File content is not valid base64")
    if len(content) > MAX_FILE_BYTES:
        raise BadRequestError("File is too large (max 15 MB)")
    return content


@router.get("", summary="List a project's test reports")
async def list_reports(project_id: str = Query(...), identity: IdentityContext = Depends(require_permission("test_report.view"))) -> dict:
    ensure_project_access(identity, project_id)
    reports = await TestReport.find({"project_id": PydanticObjectId(_oid(project_id, "project_id"))}).sort("-created_at").to_list()
    return success_response(data={"items": [_report_out(r) for r in reports]})


@router.post("/sheets", summary="List the sheets in an uploaded Excel file")
async def sheets(body: SheetsBody, identity: IdentityContext = Depends(require_permission("test_report.manage"))) -> dict:
    content = _decode(body.content_b64)
    try:
        names = parser.sheet_names(content, body.file_name)
    except Exception:
        raise BadRequestError("Could not read the Excel file")
    default = parser.PREFERRED_SHEET if parser.PREFERRED_SHEET in names else (names[0] if names else None)
    return success_response(data={"sheets": names, "default": default})


@router.post("", summary="Create a test report from a CSV / Excel file", status_code=200)
async def create_report(body: CreateBody, identity: IdentityContext = Depends(require_permission("test_report.manage"))) -> dict:
    ensure_project_access(identity, body.project_id)
    content = _decode(body.content_b64)
    headers, rows, sheet, numbers = parser.parse(content, body.file_name, body.sheet)
    columns = [Column(key=f"c{i + 1}", label=h, source_col=i + 1) for i, h in enumerate(headers)]
    keep_original = body.file_name.lower().endswith((".xlsx", ".xlsm"))
    report = TestReport(
        project_id=PydanticObjectId(_oid(body.project_id, "project_id")), name=body.name.strip(), source_file=body.file_name,
        sheet=sheet, columns=columns, row_count=len(rows), created_by=identity.user_id,
        header_row=numbers[0], source_width=len(headers), source_rows=numbers[1:], has_original=keep_original,
    )
    await report.insert()
    if keep_original:
        await files_coll().insert_one({"report_id": report.id, "project_id": report.project_id, "content": content})
    docs = [
        {"report_id": report.id, "project_id": report.project_id, "position": i, "source_row": numbers[i + 1], "data": {c.key: row[j] for j, c in enumerate(columns)}}
        for i, row in enumerate(rows)
    ]
    for start in range(0, len(docs), 1000):
        await rows_coll().insert_many(docs[start : start + 1000])
    return success_response(code=201, message="Test report created", data=_report_out(report))


@router.get("/{report_id}", summary="One test report (name + columns)")
async def get_report(report_id: str, identity: IdentityContext = Depends(require_permission("test_report.view"))) -> dict:
    return success_response(data=_report_out(await _report(identity, report_id)))


@router.patch("/{report_id}", summary="Rename a test report")
async def rename_report(report_id: str, body: RenameBody, identity: IdentityContext = Depends(require_permission("test_report.manage"))) -> dict:
    r = await _report(identity, report_id)
    r.name, r.updated_at = body.name.strip(), utcnow()
    await r.save()
    return success_response(data=_report_out(r))


@router.delete("/{report_id}", summary="Delete a test report and its rows")
async def delete_report(report_id: str, identity: IdentityContext = Depends(require_permission("test_report.manage"))) -> dict:
    r = await _report(identity, report_id)
    await rows_coll().delete_many({"report_id": r.id})
    await files_coll().delete_many({"report_id": r.id})
    await r.delete()
    return success_response(message="Test report deleted", data={"id": report_id})


@router.get("/{report_id}/export", summary="Export as .xlsx with the original colours/formatting and all edits (base64 in the envelope)")
async def export_report(report_id: str, identity: IdentityContext = Depends(require_permission("test_report.view"))) -> dict:
    r = await _report(identity, report_id)
    docs = await rows_coll().find({"report_id": r.id}).sort("position", 1).to_list(parser.MAX_ROWS + 1000)
    original = None
    if r.has_original:
        f = await files_coll().find_one({"report_id": r.id})
        original = bytes(f["content"]) if f else None
    content = exporter.build_workbook(r, [{"source_row": d.get("source_row"), "data": d.get("data", {})} for d in docs], original)
    return success_response(data={"file_name": exporter.safe_file_name(r.name), "content_b64": base64.b64encode(content).decode()})


@router.get("/{report_id}/rows", summary="Rows of a test report (paginated, optional text search)")
async def list_rows(
    report_id: str,
    q: str = Query("", max_length=100),
    f: list[str] = Query(default_factory=list, max_length=30, description="Exact column filters as `<column key>:<value>` (e.g. `c21:Pass`); repeat to combine."),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    identity: IdentityContext = Depends(require_permission("test_report.view")),
) -> dict:
    r = await _report(identity, report_id)
    flt: dict = {"report_id": r.id}
    if q.strip():
        rx = {"$regex": re.escape(q.strip()), "$options": "i"}
        flt["$or"] = [{f"data.{c.key}": rx} for c in r.columns]
    keys = {c.key for c in r.columns}
    for item in f:
        key, _, value = item.partition(":")
        if key not in keys:
            raise BadRequestError(f"Unknown column '{key}'")
        flt[f"data.{key}"] = value
    total = await rows_coll().count_documents(flt)
    docs = await rows_coll().find(flt).sort("position", 1).skip(skip).limit(limit).to_list(limit)
    return success_response(data={"items": [_row_out(d) for d in docs], "total": total, "skip": skip, "limit": limit})


@router.get("/{report_id}/columns/{key}/values", summary="Distinct values of a column (for filter dropdowns)")
async def column_values(report_id: str, key: str, identity: IdentityContext = Depends(require_permission("test_report.view"))) -> dict:
    r = await _report(identity, report_id)
    if not any(c.key == key for c in r.columns):
        raise NotFoundError("Column not found")
    values = await rows_coll().distinct(f"data.{key}", {"report_id": r.id})
    return success_response(data={"values": sorted(v for v in values if isinstance(v, str) and len(v) <= 100)[:200]})


@router.post("/{report_id}/rows", summary="Add a row")
async def add_row(report_id: str, body: RowBody = Body(default_factory=RowBody), identity: IdentityContext = Depends(require_permission("test_report.manage"))) -> dict:
    r = await _report(identity, report_id)
    last = await rows_coll().find({"report_id": r.id}).sort("position", -1).limit(1).to_list(1)
    data = {c.key: "" for c in r.columns} | _clean_data(body.data, r.columns)
    doc = {"report_id": r.id, "project_id": r.project_id, "position": (last[0]["position"] + 1) if last else 0, "data": data}
    res = await rows_coll().insert_one(doc)
    doc["_id"] = res.inserted_id
    r.row_count, r.updated_at = r.row_count + 1, utcnow()
    await r.save()
    return success_response(code=201, data=_row_out(doc))


async def _row(identity: IdentityContext, row_id: str) -> tuple[dict, TestReport]:
    doc = await rows_coll().find_one({"_id": _oid(row_id, "row_id")})
    if doc is None:
        raise NotFoundError("Row not found")
    ensure_project_access(identity, str(doc["project_id"]))
    r = await TestReport.get(doc["report_id"])
    if r is None:
        raise NotFoundError("Test report not found")
    return doc, r


@router.patch("/rows/{row_id}", summary="Edit cells of a row (only the given columns change)")
async def update_row(row_id: str, body: RowBody, identity: IdentityContext = Depends(require_permission("test_report.manage"))) -> dict:
    doc, r = await _row(identity, row_id)
    changes = _clean_data(body.data, r.columns)
    if changes:
        await rows_coll().update_one({"_id": doc["_id"]}, {"$set": {f"data.{k}": v for k, v in changes.items()}})
        await TestReport.find_one({"_id": r.id}).update({"$set": {"updated_at": utcnow()}})
    doc["data"] = {**doc.get("data", {}), **changes}
    return success_response(data=_row_out(doc))


@router.delete("/rows/{row_id}", summary="Delete a row")
async def delete_row(row_id: str, identity: IdentityContext = Depends(require_permission("test_report.manage"))) -> dict:
    doc, r = await _row(identity, row_id)
    await rows_coll().delete_one({"_id": doc["_id"]})
    r.row_count, r.updated_at = max(0, r.row_count - 1), utcnow()
    await r.save()
    return success_response(message="Row deleted", data={"id": row_id})


@router.post("/{report_id}/columns", summary="Add a column (optionally pre-filled)")
async def add_column(report_id: str, body: ColumnBody, identity: IdentityContext = Depends(require_permission("test_report.manage"))) -> dict:
    r = await _report(identity, report_id)
    label = _clean_label(body.label)
    if label.lower() in (c.label.lower() for c in r.columns):
        raise BadRequestError("A column with this name already exists")
    if len(r.columns) >= parser.MAX_COLUMNS:
        raise BadRequestError(f"Too many columns (max {parser.MAX_COLUMNS})")
    n = max((int(c.key[1:]) for c in r.columns if c.key[1:].isdigit()), default=0) + 1
    col = Column(key=f"c{n}", label=label)
    r.columns.append(col)
    r.updated_at = utcnow()
    await r.save()
    await rows_coll().update_many({"report_id": r.id}, {"$set": {f"data.{col.key}": body.default[:MAX_CELL]}})
    return success_response(code=201, data=_report_out(r))


@router.patch("/{report_id}/columns/{key}", summary="Rename a column")
async def rename_column(report_id: str, key: str, body: LabelBody, identity: IdentityContext = Depends(require_permission("test_report.manage"))) -> dict:
    r = await _report(identity, report_id)
    label = _clean_label(body.label)
    col = next((c for c in r.columns if c.key == key), None)
    if col is None:
        raise NotFoundError("Column not found")
    if any(c.key != key and c.label.lower() == label.lower() for c in r.columns):
        raise BadRequestError("A column with this name already exists")
    col.label, r.updated_at = label, utcnow()
    await r.save()
    return success_response(data=_report_out(r))


@router.delete("/{report_id}/columns/{key}", summary="Delete a column and its values")
async def delete_column(report_id: str, key: str, identity: IdentityContext = Depends(require_permission("test_report.manage"))) -> dict:
    r = await _report(identity, report_id)
    if not any(c.key == key for c in r.columns):
        raise NotFoundError("Column not found")
    if len(r.columns) == 1:
        raise BadRequestError("A report needs at least one column")
    r.columns = [c for c in r.columns if c.key != key]
    r.updated_at = utcnow()
    await r.save()
    await rows_coll().update_many({"report_id": r.id}, {"$unset": {f"data.{key}": ""}})
    return success_response(data=_report_out(r))
