"""CSV / Excel bytes -> (columns, rows). Every cell becomes text (big ids like Sender ID lose no precision)."""

import csv
import io
from datetime import date, datetime

from common.errors import BadRequestError

PREFERRED_SHEET = "Testing Sheet"
MAX_ROWS = 20000
MAX_COLUMNS = 200


def _text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.date().isoformat() if v.time() == datetime.min.time() else v.isoformat(sep=" ")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip() if isinstance(v, str) else str(v)


def _shape(grid: list[list[str]]) -> tuple[list[str], list[list[str]], list[int]]:
    """-> (headers, rows, 1-based sheet row number of the header followed by each data row)."""
    numbered = [(i + 1, r) for i, r in enumerate(grid) if any(c for c in r)]
    grid = [r for _, r in numbered]
    numbers = [n for n, _ in numbered]
    if len(grid) < 2:
        raise BadRequestError("The file needs a header row and at least one data row")
    width = min(max(len(r) for r in grid), MAX_COLUMNS)
    # drop trailing columns that are empty everywhere
    while width > 1 and not any(len(r) >= width and r[width - 1] for r in grid):
        width -= 1
    headers: list[str] = []
    for i in range(width):
        label = (grid[0][i] if i < len(grid[0]) else "").strip() or f"Column {i + 1}"
        base, n = label, 2
        while label in headers:
            label, n = f"{base} ({n})", n + 1
        headers.append(label)
    rows = [[(r[i] if i < len(r) else "") for i in range(width)] for r in grid[1:]]
    if len(rows) > MAX_ROWS:
        raise BadRequestError(f"Too many rows (max {MAX_ROWS})")
    return headers, rows, numbers


def sheet_names(content: bytes, file_name: str) -> list[str]:
    if file_name.lower().endswith(".csv"):
        return []
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    return [n for n in wb.sheetnames if not n.startswith("_")]


def parse(content: bytes, file_name: str, sheet: str | None = None) -> tuple[list[str], list[list[str]], str | None, list[int]]:
    lower = file_name.lower()
    if lower.endswith(".csv"):
        try:
            txt = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise BadRequestError("CSV must be UTF-8 encoded")
        headers, rows, numbers = _shape([[c.strip() for c in r] for r in csv.reader(io.StringIO(txt))])
        return headers, rows, None, numbers
    if not lower.endswith((".xlsx", ".xlsm")):
        raise BadRequestError("Upload a .csv or .xlsx file")
    import openpyxl

    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception:
        raise BadRequestError("Could not read the Excel file")
    names = [n for n in wb.sheetnames if not n.startswith("_")]
    chosen = sheet or (PREFERRED_SHEET if PREFERRED_SHEET in names else (names[0] if names else None))
    if chosen not in wb.sheetnames:
        raise BadRequestError(f"Sheet '{chosen}' not found", {"sheets": names})
    grid = [[_text(c) for c in row] for row in wb[chosen].iter_rows(values_only=True)]
    headers, rows, numbers = _shape(grid)
    return headers, rows, chosen, numbers
