"""Test Reports: QA test sheets (CSV / Excel) imported into Mongo.

- `test_reports`      one document per report: name, source file, ordered column list ({key, label}).
- `test_report_rows`  one document per sheet row: `report_id`, `project_id`, `position`, `source_row` (sheet row it came from), `data` {column key: text}.
                      - `test_report_files` the original .xlsx bytes, used by the export to keep colours and formatting.
                      Raw collection (free-form columns), keyed `c1, c2, ...` so labels can be renamed
                      and added without touching stored rows.
"""

from datetime import datetime, timezone

from beanie import Document, PydanticObjectId
from pydantic import BaseModel, Field
from pymongo import IndexModel

ROWS_COLLECTION = "test_report_rows"
FILES_COLLECTION = "test_report_files"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Column(BaseModel):
    key: str
    label: str
    source_col: int | None = None  # 1-based column in the uploaded sheet; None for columns added here


class TestReport(Document):
    __test__ = False  # not a pytest class

    project_id: PydanticObjectId
    name: str
    source_file: str | None = None
    sheet: str | None = None
    header_row: int | None = None  # 1-based sheet row holding the column names
    source_width: int = 0  # number of columns in the uploaded sheet
    source_rows: list[int] = Field(default_factory=list)  # sheet row numbers of the imported data rows
    has_original: bool = False  # the uploaded .xlsx is kept so an export can keep its colours/formatting
    columns: list[Column] = Field(default_factory=list)
    row_count: int = 0
    created_by: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "test_reports"
        indexes = [IndexModel([("project_id", 1), ("created_at", -1)])]
