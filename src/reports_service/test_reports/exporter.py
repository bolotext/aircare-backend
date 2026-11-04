"""Export a test report back to .xlsx.

When the upload was an .xlsx the original workbook is reopened and only the edits are applied to it, so fills,
fonts, borders, widths, other sheets and untouched cell types are kept exactly. Added columns/rows copy the
style of their neighbours; deleted ones are removed. Anything else (CSV uploads) gets a plain styled sheet.
"""

import io
import re
from copy import copy

import openpyxl
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from reports_service.test_reports.parser import _text


def _write(cell, text: str) -> None:
    try:
        if _text(cell.value) == text:
            return  # unchanged: keep the original value/type
        if text == "":
            cell.value = None
        elif isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool):
            try:
                cell.value = float(text) if "." in text else int(text)
            except ValueError:
                cell.value = text
        else:
            cell.value = text
    except AttributeError:  # part of a merged range: read-only
        pass


def safe_file_name(name: str) -> str:
    return (re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", name).strip(" .") or "test_report")[:120] + ".xlsx"


def build_workbook(report, rows: list[dict], original: bytes | None) -> bytes:
    columns = report.columns
    out = io.BytesIO()
    if original is None or not report.sheet or not report.header_row:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = re.sub(r"[\[\]:*?/\\]", "_", report.name)[:31] or "Test Report"
        ws.append([c.label for c in columns])
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for r in rows:
            ws.append([r["data"].get(c.key, "") for c in columns])
        wb.save(out)
        return out.getvalue()

    wb = openpyxl.load_workbook(io.BytesIO(original))
    ws = wb[report.sheet]
    hdr = report.header_row
    width = report.source_width
    # where each current column lives in the sheet; new columns go after the original ones
    col_at: dict[str, int] = {}
    nxt = width + 1
    for c in columns:
        if c.source_col:
            col_at[c.key] = c.source_col
        else:
            col_at[c.key] = nxt
            nxt += 1

    last_row = report.source_rows[-1] if report.source_rows else hdr
    for c in columns:
        idx = col_at[c.key]
        if not c.source_col:  # added column: header + body copy the last original column's look
            for rnum in [hdr, *report.source_rows]:
                src = ws.cell(row=rnum, column=width)
                dst = ws.cell(row=rnum, column=idx)
                if src.has_style:
                    dst._style = copy(src._style)
            src_dim = ws.column_dimensions[get_column_letter(width)]
            ws.column_dimensions[get_column_letter(idx)].width = src_dim.width or 18
        _write(ws.cell(row=hdr, column=idx), c.label)

    kept: set[int] = set()
    new_row = last_row + 1  # reuse the blank formatted rows below the data
    for r in rows:
        rnum = r.get("source_row")
        if rnum:
            kept.add(rnum)
        else:  # added row: copy the last data row's look
            rnum = new_row
            new_row += 1
            for c in columns:
                src = ws.cell(row=last_row, column=col_at[c.key])
                dst = ws.cell(row=rnum, column=col_at[c.key])
                if src.has_style:
                    dst._style = copy(src._style)
        for c in columns:
            _write(ws.cell(row=rnum, column=col_at[c.key]), r["data"].get(c.key, ""))

    for rnum in sorted(set(report.source_rows) - kept, reverse=True):
        ws.delete_rows(rnum)
    for idx in sorted(set(range(1, width + 1)) - {c.source_col for c in columns if c.source_col}, reverse=True):
        ws.delete_cols(idx)
    wb.save(out)
    return out.getvalue()
