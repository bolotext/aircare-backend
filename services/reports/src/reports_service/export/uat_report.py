"""Fills the Unit Test Report (UAT) workbook template from selected calls.

One row = one call, starting at the template's first data row. Only the
columns the template owner asked to be call-derived are written:

- Language   <- call.language
- Stage Name <- call.disposition
- Sender ID  <- call.conversation_id

Scenarios is the one tester-entered column: it is filled from the optional
`scenarios` map (conversation_id -> text). Everything else (Call Path, comments) is left as the template has it;
ratings default to Pass / N/A / Human-like on every call row so testers only change exceptions — except the
template's grey example row text and any unused rows, which are cleared so
they can't inflate the sheet's summary counts. Overall Call Score and the
summary sheet are formulas and recalculate when the file is opened.
"""

from io import BytesIO
from pathlib import Path

from openpyxl import load_workbook

from reports_service.calls.model import Call

TEMPLATE_PATH = Path(__file__).parent / "templates" / "Unit_Test_Report_Template.xlsx"
SHEET_NAME = "UAT Test sheet"
FIRST_DATA_ROW = 11
LAST_DATA_ROW = 70  # the summary sheet's formulas cover rows 11..70
MAX_CALLS = 20  # product limit on one export; the template itself holds 60

_COL_SL_NO, _COL_LANGUAGE, _COL_STAGE, _COL_SCENARIO, _COL_SENDER, _COL_COMMENTS = "A", "B", "C", "D", "F", "T"
# Rating defaults written on every call row (rows past the template's 10
# pre-filled ones would otherwise be blank): Flow/Agent/Stage Code and
# Latency..Post Barge-in = Pass, the two language columns = N/A, persona = Human-like.
_DEFAULT_RATINGS = {**{c: "Pass" for c in "GHIKLMNOP"}, "Q": "N/A", "R": "N/A", "S": "Human-like"}
# Values for the header block's "Agent Name:" / "Language:" / "Tested By:" /
# "Test Date:" labels (column E, rows 5-8).
_INFO_VALUE_COL = "G"  # one column skipped after the label in E
_INFO_ROWS = {"agent_name": 5, "language": 6, "tested_by": 7, "test_date": 8}
# Column J (Overall Call Score) is a formula and must never be cleared.
_CLEARABLE = "ABCDEFGHIKLMNOPQRST"


def _set_text(cell, value: str | None) -> None:
    """Writes `value` as plain text. openpyxl would otherwise store a string
    starting with `=` as a formula, which would let a typed scenario or a
    stored id run as a spreadsheet formula."""

    cell.value = value
    if isinstance(value, str):
        cell.data_type = "s"


def build_uat_xlsx(
    calls: list[Call], scenarios: dict[str, str] | None = None, info: dict[str, str | None] | None = None
) -> bytes:
    scenarios = scenarios or {}
    if len(calls) > MAX_CALLS:
        raise ValueError(f"At most {MAX_CALLS} calls fit in one report")

    workbook = load_workbook(TEMPLATE_PATH)
    sheet = workbook[SHEET_NAME]

    for key, row in _INFO_ROWS.items():
        _set_text(sheet[f"{_INFO_VALUE_COL}{row}"], ((info or {}).get(key) or "").strip() or None)

    for offset, call in enumerate(calls):
        row = FIRST_DATA_ROW + offset
        for column, rating in _DEFAULT_RATINGS.items():
            sheet[f"{column}{row}"] = rating
        sheet[f"{_COL_SL_NO}{row}"] = offset + 1
        _set_text(sheet[f"{_COL_LANGUAGE}{row}"], call.language)
        _set_text(sheet[f"{_COL_STAGE}{row}"], call.disposition)
        _set_text(sheet[f"{_COL_SENDER}{row}"], call.conversation_id)
        scenario = (scenarios.get(call.conversation_id) or "").strip()
        _set_text(sheet[f"{_COL_SCENARIO}{row}"], scenario or None)

    # The template's example row carries sample free text; don't ship it.
    if calls:
        sheet[f"{_COL_COMMENTS}{FIRST_DATA_ROW}"] = None

    for row in range(FIRST_DATA_ROW + len(calls), LAST_DATA_ROW + 1):
        for column in _CLEARABLE:
            sheet[f"{column}{row}"] = None

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
