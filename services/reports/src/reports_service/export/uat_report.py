"""Fills the Unit Test Report (UAT) workbook template from selected calls.

One row = one call, starting at the template's first data row. Only the
columns the template owner asked to be call-derived are written:

- Language   <- call.language
- Stage Name <- call.disposition
- Sender ID  <- call.conversation_id

Everything else (Scenarios, Call Path, the Pass/Fail ratings, comments) is
left as the template has it so testers rate each call themselves — except the
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
MAX_CALLS = 10  # product limit on one export; the template itself holds 60

_COL_SL_NO, _COL_LANGUAGE, _COL_STAGE, _COL_SCENARIO, _COL_SENDER, _COL_COMMENTS = "A", "B", "C", "D", "F", "T"
# Column J (Overall Call Score) is a formula and must never be cleared.
_CLEARABLE = "ABCDEFGHIKLMNOPQRST"


def build_uat_xlsx(calls: list[Call]) -> bytes:
    if len(calls) > MAX_CALLS:
        raise ValueError(f"At most {MAX_CALLS} calls fit in one report")

    workbook = load_workbook(TEMPLATE_PATH)
    sheet = workbook[SHEET_NAME]

    for offset, call in enumerate(calls):
        row = FIRST_DATA_ROW + offset
        sheet[f"{_COL_SL_NO}{row}"] = offset + 1
        sheet[f"{_COL_LANGUAGE}{row}"] = call.language
        sheet[f"{_COL_STAGE}{row}"] = call.disposition
        sheet[f"{_COL_SENDER}{row}"] = call.conversation_id

    # The template's example row carries sample free text; don't ship it.
    if calls:
        sheet[f"{_COL_SCENARIO}{FIRST_DATA_ROW}"] = None
        sheet[f"{_COL_COMMENTS}{FIRST_DATA_ROW}"] = None

    for row in range(FIRST_DATA_ROW + len(calls), LAST_DATA_ROW + 1):
        for column in _CLEARABLE:
            sheet[f"{column}{row}"] = None

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
