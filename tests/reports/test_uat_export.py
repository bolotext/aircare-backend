from io import BytesIO

import pytest
from beanie import PydanticObjectId
from openpyxl import load_workbook

from reports_service.core.deps import get_current_identity
from tests.reports.factories import make_call, make_identity

pytestmark = pytest.mark.asyncio

PROJECT = PydanticObjectId()
ORG = PydanticObjectId()


async def test_selected_calls_fill_language_stage_and_sender_id(app, client):
    await make_call(PROJECT, ORG, conversation_id="c-1", language="hi-IN", disposition="RESOLVED")
    await make_call(PROJECT, ORG, conversation_id="c-2", language="en-IN", disposition="ABANDONED")
    await make_call(PROJECT, ORG, conversation_id="c-3", language="kn-IN", disposition="NEUTRAL")
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")

    resp = await client.post(
        "/api/v1/export.xlsx", params={"project_id": str(PROJECT)}, json={"conversation_ids": ["c-2", "c-1"], "scenarios": {"c-2": "  Valid PTP date ", "c-1": "=1+1"}},
    )
    assert resp.status_code == 200
    sheet = load_workbook(BytesIO(resp.content))["UAT Test sheet"]
    assert [sheet.cell(11, c).value for c in (1, 2, 3, 6)] == [1, "en-IN", "ABANDONED", "c-2"]
    assert [sheet.cell(12, c).value for c in (1, 2, 3, 6)] == [2, "hi-IN", "RESOLVED", "c-1"]
    assert sheet.cell(11, 4).value == "Valid PTP date"
    # a scenario starting with "=" is stored as text, never as a formula
    assert sheet.cell(12, 4).value == "=1+1" and sheet.cell(12, 4).data_type == "s"
    assert sheet.cell(13, 6).value is None  # unused rows are cleared
    assert "c-3" not in str([[c.value for c in row] for row in sheet.iter_rows()])


async def test_more_than_ten_calls_is_rejected_and_viewer_is_forbidden(app, client):
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")
    too_many = await client.post(
        "/api/v1/export.xlsx", params={"project_id": str(PROJECT)}, json={"conversation_ids": [f"c-{i}" for i in range(21)]}
    )
    assert too_many.json()["code"] == 422 or too_many.status_code == 422

    app.dependency_overrides[get_current_identity] = lambda: make_identity("VIEWER", project_ids=[str(PROJECT)])
    forbidden = await client.post("/api/v1/export.xlsx", params={"project_id": str(PROJECT)}, json={"conversation_ids": ["c-1"]})
    assert forbidden.json()["code"] == 403


async def test_scenario_is_saved_listed_exported_and_clearable(app, client):
    await make_call(PROJECT, ORG, conversation_id="s-1", language="hi-IN", disposition="RESOLVED")
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")
    params = {"project_id": str(PROJECT)}

    saved = await client.put("/api/v1/calls/s-1/scenario", params=params, json={"scenario": "  Valid PTP date  "})
    assert saved.json()["data"]["scenario"] == "Valid PTP date"

    listed = await client.get("/api/v1/calls", params=params)
    assert [i["scenario"] for i in listed.json()["data"]["items"] if i["conversation_id"] == "s-1"] == ["Valid PTP date"]

    from reports_service.calls.model import Call

    report_doc = await Call.get_pymongo_collection().find_one({"conversation_id": "s-1"})
    assert report_doc["scenarios"] == "Valid PTP date"
    assert report_doc["scenarios_updated_by"] == "000000000000000000000001"
    assert report_doc["scenarios_updated_at"] is not None

    resp = await client.post("/api/v1/export.xlsx", params=params, json={"conversation_ids": ["s-1"]})
    assert load_workbook(BytesIO(resp.content))["UAT Test sheet"].cell(11, 4).value == "Valid PTP date"

    await client.put("/api/v1/calls/s-1/scenario", params=params, json={"scenario": ""})
    listed = await client.get("/api/v1/calls", params=params)
    assert listed.json()["data"]["items"][0]["scenario"] is None
    assert "scenarios" not in await Call.get_pymongo_collection().find_one({"conversation_id": "s-1"})

    missing = await client.put("/api/v1/calls/nope/scenario", params=params, json={"scenario": "x"})
    assert missing.json()["code"] == 404

    app.dependency_overrides[get_current_identity] = lambda: make_identity("VIEWER", project_ids=[str(PROJECT)])
    forbidden = await client.put("/api/v1/calls/s-1/scenario", params=params, json={"scenario": "x"})
    assert forbidden.json()["code"] == 403


async def test_lookup_returns_current_scenarios_in_requested_order(app, client):
    await make_call(PROJECT, ORG, conversation_id="l-1", language="hi-IN", disposition="RESOLVED")
    await make_call(PROJECT, ORG, conversation_id="l-2", language="en-IN", disposition="ABANDONED")
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")
    params = {"project_id": str(PROJECT)}
    await client.put("/api/v1/calls/l-2/scenario", params=params, json={"scenario": "Callback"})

    resp = await client.post("/api/v1/calls/lookup", params=params, json={"conversation_ids": ["l-2", "l-1", "missing"]})
    items = resp.json()["data"]
    assert [(i["conversation_id"], i["scenario"]) for i in items] == [("l-2", "Callback"), ("l-1", None)]


async def test_ratings_default_on_every_row_and_header_block_is_filled(app, client):
    for i in range(12):
        await make_call(PROJECT, ORG, conversation_id=f"d-{i}", language="hi-IN", disposition="RESOLVED")
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")
    resp = await client.post(
        "/api/v1/export.xlsx",
        params={"project_id": str(PROJECT)},
        json={"conversation_ids": [f"d-{i}" for i in range(12)], "agent_name": "Bot A"},
    )
    sheet = load_workbook(BytesIO(resp.content))["UAT Test sheet"]
    for row in (11, 20, 22):  # row 11 example had Fails; 21+ had no template defaults
        assert [sheet.cell(row, c).value for c in (7, 8, 9, 11, 12, 13, 14, 15, 16, 17, 18, 19)] == [
            "Pass", "Pass", "Pass", "Pass", "Pass", "Pass", "Pass", "Pass", "Pass", "N/A", "N/A", "Human-like"
        ]
    assert [sheet[f"G{r}"].value for r in (5, 6, 7)] == ["Bot A", "hi-IN", "Narendra"]
    assert sheet["G8"].value
