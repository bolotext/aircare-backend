from io import BytesIO

import pytest
from beanie import PydanticObjectId
from openpyxl import load_workbook

from reports_service.core.deps import get_current_identity
from tests.factories import make_call, make_identity

pytestmark = pytest.mark.asyncio

PROJECT = PydanticObjectId()
ORG = PydanticObjectId()


async def test_selected_calls_fill_language_stage_and_sender_id(app, client):
    await make_call(PROJECT, ORG, conversation_id="c-1", language="hi-IN", disposition="RESOLVED")
    await make_call(PROJECT, ORG, conversation_id="c-2", language="en-IN", disposition="ABANDONED")
    await make_call(PROJECT, ORG, conversation_id="c-3", language="kn-IN", disposition="NEUTRAL")
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    resp = await client.post(
        "/api/v1/export.xlsx", params={"project_id": str(PROJECT)}, json={"conversation_ids": ["c-2", "c-1"]}
    )
    assert resp.status_code == 200
    sheet = load_workbook(BytesIO(resp.content))["UAT Test sheet"]
    assert [sheet.cell(11, c).value for c in (1, 2, 3, 6)] == [1, "en-IN", "ABANDONED", "c-2"]
    assert [sheet.cell(12, c).value for c in (1, 2, 3, 6)] == [2, "hi-IN", "RESOLVED", "c-1"]
    assert sheet.cell(13, 6).value is None  # unused rows are cleared
    assert "c-3" not in str([[c.value for c in row] for row in sheet.iter_rows()])


async def test_more_than_ten_calls_is_rejected_and_viewer_is_forbidden(app, client):
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")
    too_many = await client.post(
        "/api/v1/export.xlsx", params={"project_id": str(PROJECT)}, json={"conversation_ids": [f"c-{i}" for i in range(11)]}
    )
    assert too_many.json()["code"] == 422 or too_many.status_code == 422

    app.dependency_overrides[get_current_identity] = lambda: make_identity("VIEWER", project_ids=[str(PROJECT)])
    forbidden = await client.post("/api/v1/export.xlsx", params={"project_id": str(PROJECT)}, json={"conversation_ids": ["c-1"]})
    assert forbidden.json()["code"] == 403
