import base64
import io

import openpyxl

import pytest
from beanie import PydanticObjectId

from reports_service.core.deps import get_current_identity
from reports_service.core.users_client import IdentityContext

pytestmark = pytest.mark.asyncio

PROJECT = PydanticObjectId()
BASE = "/api/v1/test-reports"
CSV = "Sl. No.,Call Path,Score\n1,AGENT: hi,Pass\n2,AGENT: bye,Fail\n,,\n"


def _perms(app, *perms, project=PROJECT):
    app.dependency_overrides[get_current_identity] = lambda: IdentityContext(
        user_id="u1", organization_id=None, role="X", permissions=frozenset(perms), project_ids=frozenset({str(project)}), has_all_projects=False
    )


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


async def _create(client, name="Nov"):
    r = await client.post(BASE, json={"project_id": str(PROJECT), "name": name, "file_name": "t.csv", "content_b64": _b64(CSV)})
    assert r.json()["code"] == 201, r.json()
    return r.json()["data"]


async def test_import_edit_columns(app, client):
    _perms(app, "test_report.view", "test_report.manage")
    rep = await _create(client)
    assert [c["label"] for c in rep["columns"]] == ["Sl. No.", "Call Path", "Score"] and rep["row_count"] == 2
    rid = rep["id"]

    rows = (await client.get(f"{BASE}/{rid}/rows")).json()["data"]
    assert rows["total"] == 2 and rows["items"][0]["data"]["c2"] == "AGENT: hi"
    assert (await client.get(f"{BASE}/{rid}/rows", params={"q": "bye"})).json()["data"]["total"] == 1

    assert (await client.get(f"{BASE}/{rid}/rows", params={"f": "c3:Fail"})).json()["data"]["total"] == 1
    assert (await client.get(f"{BASE}/{rid}/rows", params={"f": ["c3:Fail", "c1:1"]})).json()["data"]["total"] == 0
    assert (await client.get(f"{BASE}/{rid}/columns/c3/values")).json()["data"]["values"] == ["Fail", "Pass"]

    row_id = rows["items"][0]["id"]
    upd = (await client.patch(f"{BASE}/rows/{row_id}", json={"data": {"c3": "Changed"}})).json()["data"]
    assert upd["data"]["c3"] == "Changed" and upd["data"]["c2"] == "AGENT: hi"
    assert (await client.patch(f"{BASE}/rows/{row_id}", json={"data": {"zz": "x"}})).json()["code"] == 400

    cols = (await client.post(f"{BASE}/{rid}/columns", json={"label": "Dev comments", "default": "-"})).json()["data"]["columns"]
    assert (cols[-1]["key"], cols[-1]["label"], cols[-1]["source_col"]) == ("c4", "Dev comments", None)
    assert (await client.post(f"{BASE}/{rid}/columns", json={"label": "dev comments"})).json()["code"] == 400
    assert (await client.get(f"{BASE}/{rid}/rows")).json()["data"]["items"][0]["data"]["c4"] == "-"
    assert (await client.patch(f"{BASE}/{rid}/columns/c4", json={"label": "Dev"})).json()["data"]["columns"][-1]["label"] == "Dev"
    assert len((await client.delete(f"{BASE}/{rid}/columns/c4")).json()["data"]["columns"]) == 3

    added = (await client.post(f"{BASE}/{rid}/rows", json={"data": {"c1": "3"}})).json()["data"]
    assert added["position"] == 2
    assert (await client.delete(f"{BASE}/rows/{added['id']}")).json()["code"] == 200
    assert (await client.get(f"{BASE}/{rid}")).json()["data"]["row_count"] == 2

    assert len((await client.get(BASE, params={"project_id": str(PROJECT)})).json()["data"]["items"]) == 1
    assert (await client.delete(f"{BASE}/{rid}")).json()["code"] == 200
    assert (await client.get(f"{BASE}/{rid}")).json()["code"] == 404


async def test_permissions_and_bad_files(app, client):
    _perms(app, "test_report.view")
    r = await client.post(BASE, json={"project_id": str(PROJECT), "name": "x", "file_name": "t.csv", "content_b64": _b64(CSV)})
    assert r.json()["code"] == 403
    _perms(app, "test_report.view", "test_report.manage")
    bad = await client.post(BASE, json={"project_id": str(PROJECT), "name": "x", "file_name": "t.pdf", "content_b64": _b64(CSV)})
    assert bad.json()["code"] == 400
    other = await client.post(BASE, json={"project_id": str(PydanticObjectId()), "name": "x", "file_name": "t.csv", "content_b64": _b64(CSV)})
    assert other.json()["code"] == 403


async def test_export_keeps_original_formatting_and_edits(app, client):
    from openpyxl.styles import PatternFill

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Testing Sheet"
    ws.append(["Sl. No.", "Call Score"])
    ws.append([1, "Pass"])
    ws.append([2, "Fail"])
    ws["B2"].fill = PatternFill("solid", fgColor="FF00FF00")
    buf = io.BytesIO()
    wb.save(buf)
    _perms(app, "test_report.view", "test_report.manage")
    r = await client.post(BASE, json={"project_id": str(PROJECT), "name": "Fmt", "file_name": "t.xlsx", "content_b64": base64.b64encode(buf.getvalue()).decode()})
    rid = r.json()["data"]["id"]
    rows = (await client.get(f"{BASE}/{rid}/rows")).json()["data"]["items"]
    await client.patch(f"{BASE}/rows/{rows[1]['id']}", json={"data": {"c2": "Edited"}})
    await client.post(f"{BASE}/{rid}/columns", json={"label": "Dev comments", "default": "ok"})
    exp = (await client.get(f"{BASE}/{rid}/export")).json()["data"]
    assert exp["file_name"] == "Fmt.xlsx"
    out = openpyxl.load_workbook(io.BytesIO(base64.b64decode(exp["content_b64"]))).active
    assert out["B2"].fill.fgColor.rgb == "FF00FF00" and out["B2"].value == "Pass"
    assert out["B3"].value == "Edited" and out["A2"].value == 1  # numeric type kept
    assert out["C1"].value == "Dev comments" and out["C2"].value == "ok"
