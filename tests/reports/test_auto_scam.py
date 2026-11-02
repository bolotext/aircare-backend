import asyncio
import json
import time

import pytest
from beanie import PydanticObjectId

from reports_service.auto_scam import runner
from reports_service.auto_scam.model import AutoScamDataset, AutoScamResult, AutoScamRun
from reports_service.auto_scam.records import records_collection
from reports_service.core.config import get_settings
from reports_service.core.deps import get_current_identity
from tests.reports.factories import make_identity

pytestmark = pytest.mark.asyncio

PROJECT = PydanticObjectId()
OTHER = PydanticObjectId()
BASE = "/api/v1/auto-scam"
P = {"project_id": str(PROJECT)}
RAW = json.dumps([
    {"_id": {"$oid": "6ae8b6994f6ebabc318ed040"}, "customerCRTId": "a", "STAGE_CODE": "NEUTRAL", "INTEREST_CLASSIFICATION": "WARM",
     "conversation_log": [{"role": "user", "content": "x"}]},
    {"customerCRTId": "b", "STAGE_CODE": "DSCN", "INTEREST_CLASSIFICATION": "COLD", "conversation_log": []},
])


@pytest.fixture(autouse=True)
def fake_scam(monkeypatch):
    """Replace the OpenAI call; record which customerCRTIds were actually analysed."""
    get_settings.cache_clear()
    state = {"calls": [], "delay": 0.0, "fail": set(), "extra": {}}

    def analyze(conversation, client, prompt_text, max_retries):
        state["calls"].append((conversation.get("customerCRTId"), prompt_text))
        time.sleep(state["delay"])
        if conversation.get("customerCRTId") in state["fail"]:
            return {"error": "boom"}
        return {"INTEREST_CLASSIFICATION": "HOT", **state["extra"]}

    monkeypatch.setattr(runner, "_make_client", lambda: object())
    monkeypatch.setattr(runner, "_analyze", analyze)
    yield state
    get_settings.cache_clear()


def _as(app, role, project=PROJECT):
    app.dependency_overrides[get_current_identity] = lambda: make_identity(role, project_ids=[str(project)])


def _perms(app, *perms):
    from reports_service.core.users_client import IdentityContext
    app.dependency_overrides[get_current_identity] = lambda: IdentityContext(
        user_id="u1", organization_id=None, role="X", permissions=frozenset(perms), project_ids=frozenset({str(PROJECT)}), has_all_projects=False
    )


def _unwrap(r):
    return r.json()["data"]


async def _upload(client, name, raw=RAW):
    return await client.post(f"{BASE}/datasets/upload", json={**P, "file_name": name, "raw_json": raw})


async def _run(client, name, force=False):
    started = _unwrap(await client.post(f"{BASE}/runs", json={**P, "file_names": [name], "force": force}))
    run_id = started["started"][0]["run_id"]
    for _ in range(100):
        run = _unwrap(await client.get(f"{BASE}/runs/{run_id}"))
        if run["status"] not in ("queued", "running"):
            return run
        await asyncio.sleep(0.1)
    raise AssertionError(f"run did not finish: {run}")


async def test_upload_crud_settings(app, client):
    _perms(app, "auto_scam.view", "auto_scam.manage", "auto_scam.run")
    assert (await _upload(client, "../x")).json()["code"] == 400
    assert (await _upload(client, "nov", "nope")).json()["code"] == 400
    assert _unwrap(await _upload(client, "nov")) == {"file_name": "nov", "records": 2}
    assert (await _upload(client, "nov")).json()["code"] == 409
    again = await client.post(f"{BASE}/datasets/upload", json={**P, "file_name": "nov", "raw_json": RAW, "overwrite": True})
    assert again.json()["code"] == 201
    # stored with project_id == file name
    assert await records_collection().count_documents({"project_id": "nov", "aircare_project_id": PROJECT}) == 2

    items = _unwrap(await client.get(f"{BASE}/datasets/nov/records", params=P))["items"]
    rid = items[0]["id"]
    rec = _unwrap(await client.get(f"{BASE}/records/{rid}"))
    rec["STAGE_CODE"] = "EDITED"
    assert (await client.put(f"{BASE}/records/{rid}", json=rec)).json()["code"] == 200
    assert _unwrap(await client.get(f"{BASE}/records/{rid}"))["STAGE_CODE"] == "EDITED"
    new = _unwrap(await client.post(f"{BASE}/datasets/nov/records", params=P, json={"customerCRTId": "c"}))["id"]
    assert (await client.delete(f"{BASE}/records/{new}")).json()["code"] == 200

    cfg = _unwrap(await client.get(f"{BASE}/datasets/nov/config", params=P))
    assert "role" in cfg["prompt"] and set(cfg) == {"prompt", "rules", "updated_columns", "workers", "max_retries"}
    assert (await client.put(f"{BASE}/datasets/nov/config", params=P, json={**cfg, "prompt": ""})).json()["code"] == 422
    assert (await client.put(f"{BASE}/datasets/nov/config", params=P, json=cfg)).json()["code"] == 200  # the real bundled prompt saves fine
    assert (await client.put(f"{BASE}/datasets/nov/config", params=P, json={**cfg, "prompt": "role: |\n  stricter\n", "workers": 3})).json()["code"] == 200
    assert "stricter" in (await AutoScamDataset.find_one({"file_name": "nov"})).config.prompt
    assert (await client.delete(f"{BASE}/datasets/nov", params=P)).json()["code"] == 200
    assert await records_collection().count_documents({"project_id": "nov"}) == 0


async def test_run_skips_already_analysed_by_customer_crt_id(app, client, fake_scam):
    _perms(app, "auto_scam.view", "auto_scam.manage", "auto_scam.run")
    await _upload(client, "nov")
    await client.put(f"{BASE}/datasets/nov/config", params=P, json={"prompt": "role: |\n  p1\n", "workers": 2, "max_retries": 0})

    run = await _run(client, "nov")
    assert (run["status"], run["total"], run["processed"], run["skipped"]) == ("succeeded", 2, 2, 0)
    assert sorted(c for c, _ in fake_scam["calls"]) == ["a", "b"]
    assert all("p1" in prompt for _, prompt in fake_scam["calls"])

    rows = _unwrap(await client.get(f"{BASE}/datasets/nov/results", params=P))["items"]
    assert {r["customerCRTId"] for r in rows} == {"a", "b"}  # keyed by customerCRTId
    a = next(r for r in rows if r["customerCRTId"] == "a")
    assert a["fields"] == [{"field": "INTEREST_CLASSIFICATION", "previous": "WARM", "updated": "HOT", "changed": True}]
    assert a["changed"] is True
    assert (a["previous_stage_code"], a["updated_stage_code"]) == ("NEUTRAL", "NEUTRAL")
    listed = _unwrap(await client.get(f"{BASE}/datasets/nov/records", params=P))
    assert listed["columns"] == [{"field": "INTEREST_CLASSIFICATION", "name": "Updated class"}]  # default, renamable in Settings
    first = listed["items"][0]
    assert first["values"] == {"INTEREST_CLASSIFICATION": "WARM"}  # original value, original field name
    assert first["result"]["updated"] == {"INTEREST_CLASSIFICATION": "HOT"} and first["result"]["changed"] == {"INTEREST_CLASSIFICATION": True}
    assert _unwrap(await client.get(f"{BASE}/datasets", params=P))[0]["analysed"] == 2

    # second run: everything already has analytics -> nothing is sent to OpenAI
    fake_scam["calls"].clear()
    run = await _run(client, "nov")
    assert (run["status"], run["total"], run["skipped"]) == ("succeeded", 0, 2)
    assert fake_scam["calls"] == [] and "already" in run["log"]

    # editing a conversation invalidates only its result
    rid = next(i["id"] for i in _unwrap(await client.get(f"{BASE}/datasets/nov/records", params=P))["items"] if i["customerCRTId"] == "b")
    rec = _unwrap(await client.get(f"{BASE}/records/{rid}"))
    rec["conversation_log"] = [{"role": "user", "content": "changed"}]
    await client.put(f"{BASE}/records/{rid}", json=rec)
    fake_scam["calls"].clear()
    run = await _run(client, "nov")
    assert (run["total"], run["skipped"]) == (1, 1) and [c for c, _ in fake_scam["calls"]] == ["b"]

    # force re-analyses everything
    fake_scam["calls"].clear()
    run = await _run(client, "nov", force=True)
    assert run["total"] == 2 and len(fake_scam["calls"]) == 2
    assert await AutoScamResult.find({"file_name": "nov"}).count() == 2  # still one row per customerCRTId
    doc = await AutoScamRun.get(PydanticObjectId(run["id"]))
    assert "p1" in doc.prompt_text and doc.force is True


async def test_failed_analysis_is_retried_and_never_overwrites_success(app, client, fake_scam):
    _perms(app, "auto_scam.view", "auto_scam.manage", "auto_scam.run")
    await _upload(client, "nov")
    fake_scam["fail"] = {"b"}
    run = await _run(client, "nov")
    assert (run["status"], run["failed"]) == ("completed_with_errors", 1) and "b: failed" in run["log"]
    rows = {r["customerCRTId"]: r for r in _unwrap(await client.get(f"{BASE}/datasets/nov/results", params=P))["items"]}
    assert rows["b"]["error"] == "boom" and rows["a"]["error"] is None

    # next run retries only the failed one
    fake_scam["fail"] = set()
    fake_scam["calls"].clear()
    run = await _run(client, "nov")
    assert run["status"] == "succeeded" and [c for c, _ in fake_scam["calls"]] == ["b"]

    # a failure under force keeps the earlier successful result
    fake_scam["fail"] = {"a"}
    await _run(client, "nov", force=True)
    rows = {r["customerCRTId"]: r for r in _unwrap(await client.get(f"{BASE}/datasets/nov/results", params=P))["items"]}
    assert rows["a"]["fields"][0]["updated"] == "HOT" and rows["a"]["error"] is None


async def test_customer_crt_id_is_indexed():
    from reports_service.auto_scam.records import ensure_indexes

    await ensure_indexes()
    await ensure_indexes()  # idempotent
    names = set((await records_collection().index_information()))
    assert {"customerCRTId_1", "project_file_customerCRTId"} <= names
    assert any("customer_crt_id" in str(v["key"]) for v in (await AutoScamResult.get_pymongo_collection().index_information()).values())


async def test_duplicate_customer_crt_id_analysed_once(app, client, fake_scam):
    _perms(app, "auto_scam.view", "auto_scam.manage", "auto_scam.run")
    dup = json.dumps([{"customerCRTId": "z", "conversation_log": []}, {"customerCRTId": "z", "conversation_log": []}])
    await _upload(client, "dups", dup)
    run = await _run(client, "dups")
    assert run["total"] == 1 and len(fake_scam["calls"]) == 1


async def test_cancel_stops_a_running_run(app, client, fake_scam):
    _perms(app, "auto_scam.view", "auto_scam.manage", "auto_scam.run")
    many = json.dumps([{"customerCRTId": f"c{i}", "conversation_log": []} for i in range(30)])
    await _upload(client, "slow", many)
    await client.put(f"{BASE}/datasets/slow/config", params=P, json={"prompt": "role: x", "workers": 1, "max_retries": 0})
    fake_scam["delay"] = 0.3
    run_id = _unwrap(await client.post(f"{BASE}/runs", json={**P, "file_names": ["slow"]}))["started"][0]["run_id"]
    for _ in range(50):
        run = _unwrap(await client.get(f"{BASE}/runs/{run_id}"))
        if run["status"] == "running" and fake_scam["calls"]:
            break
        await asyncio.sleep(0.1)
    assert run["status"] == "running"
    assert (await client.post(f"{BASE}/runs/{run_id}/cancel")).json()["code"] == 200
    for _ in range(100):
        run = _unwrap(await client.get(f"{BASE}/runs/{run_id}"))
        if run["status"] not in ("queued", "running"):
            break
        await asyncio.sleep(0.1)
    assert run["status"] == "cancelled", run
    assert len(fake_scam["calls"]) < 30


async def test_bulk_delete(app, client):
    _perms(app, "auto_scam.view", "auto_scam.manage")
    for n in ("f1", "f2", "f3"):
        await _upload(client, n)
    items = _unwrap(await client.get(f"{BASE}/datasets/f1/records", params=P))["items"]
    other = _unwrap(await client.get(f"{BASE}/datasets/f2/records", params=P))["items"][0]["id"]
    r = await client.post(f"{BASE}/datasets/f1/records/bulk-delete", json={**P, "record_ids": [items[0]["id"], other]})
    assert _unwrap(r)["deleted"] == 1  # an id from another dataset is ignored
    assert _unwrap(await client.get(f"{BASE}/datasets/f2/records", params=P))["total"] == 2
    assert (await client.post(f"{BASE}/datasets/f1/records/bulk-delete", json={**P, "record_ids": []})).json()["code"] == 400
    r = await client.post(f"{BASE}/datasets/f2/records/bulk-delete", json={**P, "all_matching": True, "q": "DSCN"})
    assert _unwrap(r)["deleted"] == 1
    r = await client.post(f"{BASE}/datasets/f2/records/bulk-delete", json={**P, "all_matching": True})
    assert _unwrap(r)["deleted"] == 1
    r = _unwrap(await client.post(f"{BASE}/datasets/bulk-delete", json={**P, "file_names": ["f1", "f3", "ghost"]}))
    assert sorted(r["deleted"]) == ["f1", "f3"] and r["skipped"][0]["reason"] == "not found"
    assert [d["file_name"] for d in _unwrap(await client.get(f"{BASE}/datasets", params=P))] == ["f2"]
    _perms(app, "auto_scam.view")
    assert (await client.post(f"{BASE}/datasets/bulk-delete", json={**P, "file_names": ["f2"]})).json()["code"] == 403


@pytest.mark.filterwarnings("ignore")
async def test_s3_and_auto_scam_permissions_are_admin_only():
    from users_service.db.seed import ROLE_PERMISSIONS
    from common.enums import Role

    for role in (Role.SUPERVISOR, Role.VIEWER):
        assert not [p for p in ROLE_PERMISSIONS[role] if p.startswith(("s3.", "auto_scam."))]
    assert ROLE_PERMISSIONS[Role.ADMIN] == ["*"]


async def test_permissions_and_project_isolation(app, client):
    for role in ("VIEWER", "SUPERVISOR"):
        _as(app, role)
        assert (await client.get(f"{BASE}/datasets", params=P)).json()["code"] == 403
        assert (await client.post(f"{BASE}/runs", json={**P, "file_names": ["x"]})).json()["code"] == 403
    _perms(app, "auto_scam.view")
    assert (await _upload(client, "x")).json()["code"] == 403
    assert (await client.get(f"{BASE}/datasets", params={"project_id": str(OTHER)})).json()["code"] == 403
    _perms(app, "auto_scam.view", "auto_scam.manage")
    await _upload(client, "x")
    assert (await client.post(f"{BASE}/runs", json={**P, "file_names": ["x"]})).json()["code"] == 403


async def test_multiple_output_fields_are_shown_under_their_original_names(app, client, fake_scam):
    _perms(app, "auto_scam.view", "auto_scam.manage", "auto_scam.run")
    raw = json.dumps([{"customerCRTId": "a", "INTEREST_CLASSIFICATION": "WARM", "SENTIMENT": "positive", "conversation_log": []}])
    await _upload(client, "multi", raw)
    fake_scam["extra"] = {"SENTIMENT": "positive", "NEXT_STEP": "call back"}  # 3 outputs; SENTIMENT unchanged
    cfg = _unwrap(await client.get(f"{BASE}/datasets/multi/config", params=P))
    cols = [{"field": "INTEREST_CLASSIFICATION", "name": "New lead score"}, {"field": "NEXT_STEP", "name": "Next step"}]
    assert (await client.put(f"{BASE}/datasets/multi/config", params=P, json={**cfg, "updated_columns": cols})).json()["code"] == 200
    dup = [cols[0], {"field": "INTEREST_CLASSIFICATION", "name": "again"}]
    assert (await client.put(f"{BASE}/datasets/multi/config", params=P, json={**cfg, "updated_columns": dup})).json()["code"] == 422
    blank = [{"field": "SENTIMENT", "name": "  "}]
    assert (await client.put(f"{BASE}/datasets/multi/config", params=P, json={**cfg, "updated_columns": blank})).json()["code"] == 422
    await _run(client, "multi")
    row = _unwrap(await client.get(f"{BASE}/datasets/multi/results", params=P))["items"][0]
    by = {f["field"]: f for f in row["fields"]}
    assert set(by) == {"INTEREST_CLASSIFICATION", "SENTIMENT", "NEXT_STEP"}
    assert by["INTEREST_CLASSIFICATION"]["changed"] is True
    assert by["SENTIMENT"] == {"field": "SENTIMENT", "previous": "positive", "updated": "positive", "changed": False}
    assert by["NEXT_STEP"] == {"field": "NEXT_STEP", "previous": None, "updated": "call back", "changed": True}
    listed = _unwrap(await client.get(f"{BASE}/datasets/multi/records", params=P))
    assert listed["columns"] == cols  # only what the user chose, under the names they gave
    item = listed["items"][0]
    assert item["values"] == {"INTEREST_CLASSIFICATION": "WARM", "NEXT_STEP": None}
    assert item["result"]["updated"] == {"INTEREST_CLASSIFICATION": "HOT", "NEXT_STEP": "call back"}
    assert _unwrap(await client.get(f"{BASE}/datasets/multi/results", params=P))["columns"] == cols


async def test_run_only_the_selected_conversations(app, client, fake_scam):
    _perms(app, "auto_scam.view", "auto_scam.manage", "auto_scam.run")
    many = json.dumps([{"customerCRTId": f"c{i}", "STAGE_CODE": "S" if i < 3 else "T", "conversation_log": []} for i in range(6)])
    await _upload(client, "sel", many)
    await _upload(client, "other", many)
    items = _unwrap(await client.get(f"{BASE}/datasets/sel/records", params=P))["items"]
    ids = {i["customerCRTId"]: i["id"] for i in items}
    other_id = _unwrap(await client.get(f"{BASE}/datasets/other/records", params=P))["items"][0]["id"]

    async def go(**extra):
        body = {**P, "file_names": ["sel"], **extra}
        started = _unwrap(await client.post(f"{BASE}/runs", json=body))["started"][0]["run_id"]
        for _ in range(100):
            run = _unwrap(await client.get(f"{BASE}/runs/{started}"))
            if run["status"] not in ("queued", "running"):
                return run
            await asyncio.sleep(0.1)

    run = await go(record_ids=[ids["c1"], ids["c4"], other_id])  # an id from another file never matches
    assert (run["total"], run["selected"]) == (2, 3) and sorted(c for c, _ in fake_scam["calls"]) == ["c1", "c4"]
    rows = _unwrap(await client.get(f"{BASE}/datasets/sel/results", params=P))["items"]
    assert sorted(r["customerCRTId"] for r in rows) == ["c1", "c4"]

    # selection that includes an already-analysed one: it is skipped unless forced
    fake_scam["calls"].clear()
    run = await go(record_ids=[ids["c1"], ids["c2"]])
    assert (run["total"], run["skipped"]) == (1, 1) and [c for c, _ in fake_scam["calls"]] == ["c2"]
    fake_scam["calls"].clear()
    run = await go(record_ids=[ids["c1"]], force=True)
    assert run["total"] == 1 and [c for c, _ in fake_scam["calls"]] == ["c1"]

    # "all matching" a search (STAGE_CODE T -> c3..c5)
    fake_scam["calls"].clear()
    run = await go(all_matching=True, q="T")
    assert sorted(c for c, _ in fake_scam["calls"]) == ["c3", "c5"]  # c4 already analysed

    two = await client.post(f"{BASE}/runs", json={**P, "file_names": ["sel", "other"], "record_ids": [ids["c1"]]})
    assert two.json()["code"] == 400


SAMPLE = """[{
  "_id": {"$oid": "6ae8b6994f6ebabc318ed040"},
  "setupTime": 49, "tempDate": "26/10/2026", "STAGE_CODE": "NOT_INTERESTED", "INTEREST_CLASSIFICATION": "COLD",
  "conversation_log": [
    {"role": "assistant", "content": "வணக்கம்! நான் மாயா", "timestamp": 1792987551.061424},
    {"timestamp": 1792987567.579938, "total_results": {"ta-IN": "ஆம்"}, "detected_language": "tamil",
     "user_interruption_flag": false, "turn_id": 1, "role": "user", "content": "ஆம்"},
    {"turn_id": 1, "fullContent": "ரொம்ப நல்லது", "role": "assistant", "content": "ரொம்ப நல்லது", "timestamp": 1792987569.082959}
  ],
  "DISPOSITION": "NOT_INTERESTED",
  "customerCRTId": "47c7c1ac-a100-4396-8953-a052ced333ae"
}]"""


async def test_upload_accepts_the_real_export_shapes(app, client, fake_scam):
    _perms(app, "auto_scam.view", "auto_scam.manage", "auto_scam.run")
    # 1) the export exactly as pasted (array, Mongo $oid, Tamil text, nested logs)
    assert _unwrap(await _upload(client, "tamil", SAMPLE)) == {"file_name": "tamil", "records": 1}
    rec_id = _unwrap(await client.get(f"{BASE}/datasets/tamil/records", params=P))["items"][0]["id"]
    rec = _unwrap(await client.get(f"{BASE}/records/{rec_id}"))
    assert rec["customerCRTId"] == "47c7c1ac-a100-4396-8953-a052ced333ae" and rec["conversation_log"][1]["content"] == "ஆம்"
    assert rec["source_id"] == "6ae8b6994f6ebabc318ed040"
    run = await _run(client, "tamil")
    assert run["status"] == "succeeded" and fake_scam["calls"][0][0] == "47c7c1ac-a100-4396-8953-a052ced333ae"
    # 2) a single object, 3) JSON Lines (mongoexport)
    one = json.dumps(json.loads(SAMPLE)[0])
    assert _unwrap(await _upload(client, "single", one))["records"] == 1
    lines = "\n".join(json.dumps({"customerCRTId": f"x{i}", "conversation_log": []}) for i in range(3))
    assert _unwrap(await _upload(client, "jsonl", lines))["records"] == 3
    assert (await _upload(client, "bad", "{not json\nalso not")).json()["code"] == 400


# ------------------------------------------------------------------ dynamic rules
def test_default_rules_reproduce_the_old_hardcoded_logic():
    from itertools import product

    from reports_service.auto_scam.rules import apply_rules, default_rules
    from reports_service.auto_scam.scam import scam as scam_mod

    stages = [None, "INTERESTED", "INITIAL_MESSAGE_DELIVERED", "DSCN", "VOICEMAIL", "BUSY", "NO_ANSWER", "NEUTRAL", "NOT_INTERESTED"]
    leads = ["HOT", "WARM", "COLD", "bogus", None, ""]
    for stage, setup, lead in product(stages, [None, 0, 10, 34, 35, 36, 200], leads):
        analytics = {} if lead is None else {"INTEREST_CLASSIFICATION": lead}
        old = scam_mod._normalize_analytics(dict(analytics), setupTime=setup, stage_code=stage)["INTEREST_CLASSIFICATION"]
        new, _ = apply_rules(default_rules(), {"STAGE_CODE": stage, "setupTime": setup}, analytics)
        assert new["INTEREST_CLASSIFICATION"] == old, (stage, setup, lead)


def test_rule_operators_and_actions():
    from reports_service.auto_scam.rules import Action, Condition, Rule, apply_rules

    conv = {"STAGE_CODE": "INTERESTED", "setupTime": 20, "DISPOSITION": "NEUTRAL", "city": "Chennai"}

    def run(when, match="all", analytics=None, enabled=True):
        rule = Rule(when=when, match=match, enabled=enabled, then=[Action(field="DISPOSITION", value="X")])
        out, fired = apply_rules([rule], conv, analytics or {"INTEREST_CLASSIFICATION": "HOT"})
        return out.get("DISPOSITION") == "X", fired

    C = Condition
    assert run([C(field="setupTime", op="lt", value="35")])[0]
    assert not run([C(field="setupTime", op="gte", value=35)])[0]
    assert run([C(field="STAGE_CODE", op="in", value="INTERESTED, BUSY")])[0]  # comma-separated list
    assert run([C(field="STAGE_CODE", op="not_in", value=["BUSY"])])[0]
    assert run([C(field="city", op="contains", value="chen")])[0]  # case-insensitive
    assert run([C(field="missing", op="is_empty")])[0] and run([C(field="city", op="not_empty")])[0]
    assert run([C(field="INTEREST_CLASSIFICATION", op="eq", value="HOT")])[0]  # reads the analytics too
    assert not run([C(field="STAGE_CODE", op="eq", value="BUSY"), C(field="setupTime", op="lt", value=35)])[0]  # all
    assert run([C(field="STAGE_CODE", op="eq", value="BUSY"), C(field="setupTime", op="lt", value=35)], match="any")[0]
    assert run([])[0]  # no conditions = always
    assert not run([], enabled=False)[0]
    assert not run([C(field="STAGE_CODE", op="lt", value=5)])[0]  # non-numeric never raises, just false
    # later rules see earlier changes
    first = Rule(when=[C(field="STAGE_CODE", op="eq", value="INTERESTED")], then=[Action(field="DISPOSITION", value="STEP1")])
    second = Rule(when=[C(field="DISPOSITION", op="eq", value="STEP1")], then=[Action(field="DISPOSITION", value="STEP2")])
    assert apply_rules([first, second], conv, {})[0]["DISPOSITION"] == "STEP2"


async def test_rules_are_saved_per_dataset_and_update_disposition(app, client, fake_scam):
    _perms(app, "auto_scam.view", "auto_scam.manage", "auto_scam.run")
    raw = json.dumps([
        {"customerCRTId": "a", "STAGE_CODE": "INTERESTED", "setupTime": 10, "DISPOSITION": "NEUTRAL", "INTEREST_CLASSIFICATION": "WARM", "conversation_log": []},
        {"customerCRTId": "b", "STAGE_CODE": "DSCN", "setupTime": 90, "DISPOSITION": "NEUTRAL", "INTEREST_CLASSIFICATION": "WARM", "conversation_log": []},
    ])
    await _upload(client, "rules", raw)
    cfg = _unwrap(await client.get(f"{BASE}/datasets/rules/config", params=P))
    assert len(cfg["rules"]) == 4  # starts with the former hard-coded logic
    cfg["rules"] = cfg["rules"] + [{
        "name": "short interested calls are callbacks", "match": "all",
        "when": [{"field": "STAGE_CODE", "op": "eq", "value": "INTERESTED"}, {"field": "setupTime", "op": "lt", "value": 35}],
        "then": [{"field": "DISPOSITION", "value": "CALLBACK"}, {"field": "STAGE_CODE", "value": "CALLBACK_SCHEDULED"}],
    }]
    cfg["updated_columns"] = [
        {"field": "STAGE_CODE", "name": "New stage"}, {"field": "DISPOSITION", "name": "New disposition"},
        {"field": "INTEREST_CLASSIFICATION", "name": "New class"},
    ]
    assert (await client.put(f"{BASE}/datasets/rules/config", params=P, json=cfg)).json()["code"] == 200
    bad = {**cfg, "rules": [{"when": [{"field": "x", "op": "regex", "value": "a"}], "then": [{"field": "y", "value": 1}]}]}
    assert (await client.put(f"{BASE}/datasets/rules/config", params=P, json=bad)).json()["code"] == 422
    assert (await client.put(f"{BASE}/datasets/rules/config", params=P, json={**cfg, "rules": [{"when": [], "then": []}]})).json()["code"] == 422

    run = await _run(client, "rules")
    assert run["status"] == "succeeded"
    rows = {r["customerCRTId"]: {f["field"]: f for f in r["fields"]} for r in _unwrap(await client.get(f"{BASE}/datasets/rules/results", params=P))["items"]}
    # fake model says HOT: a (INTERESTED, 10s) -> default rule HOT->WARM, then the new rule sets DISPOSITION
    assert rows["a"]["INTEREST_CLASSIFICATION"]["updated"] == "WARM"
    assert rows["a"]["DISPOSITION"] == {"field": "DISPOSITION", "previous": "NEUTRAL", "updated": "CALLBACK", "changed": True}
    # b (DSCN) -> COLD by the default rules; the DISPOSITION rule does not apply, so there is no DISPOSITION output
    assert rows["b"]["INTEREST_CLASSIFICATION"]["updated"] == "COLD" and "DISPOSITION" not in rows["b"]
    result_rows = {r["customerCRTId"]: r for r in _unwrap(await client.get(f"{BASE}/datasets/rules/results", params=P))["items"]}
    assert (result_rows["a"]["previous_stage_code"], result_rows["a"]["updated_stage_code"]) == ("INTERESTED", "CALLBACK_SCHEDULED")
    assert (result_rows["b"]["previous_stage_code"], result_rows["b"]["updated_stage_code"]) == ("DSCN", "DSCN")  # rule didn't apply
    listed = _unwrap(await client.get(f"{BASE}/datasets/rules/records", params=P))
    assert [c["name"] for c in listed["columns"]] == ["New stage", "New disposition", "New class"]
    first = next(i for i in listed["items"] if i["customerCRTId"] == "a")
    assert first["result"]["updated"] == {"STAGE_CODE": "CALLBACK_SCHEDULED", "DISPOSITION": "CALLBACK", "INTEREST_CLASSIFICATION": "WARM"}
    assert first["values"]["STAGE_CODE"] == "INTERESTED"


async def test_rules_dry_run_endpoint(app, client):
    _perms(app, "auto_scam.view")
    body = {"rules": [{"name": "r", "when": [{"field": "STAGE_CODE", "op": "eq", "value": "DSCN"}], "then": [{"field": "DISPOSITION", "value": "DROPPED"}]}],
            "conversation": {"STAGE_CODE": "DSCN"}, "analytics": {"INTEREST_CLASSIFICATION": "HOT"}}
    data = _unwrap(await client.post(f"{BASE}/rules/test", json=body))
    assert data == {"analytics": {"INTEREST_CLASSIFICATION": "HOT", "DISPOSITION": "DROPPED"}, "fired": ["r"]}
    defaults = _unwrap(await client.get(f"{BASE}/defaults"))
    assert len(defaults["rules"]) == 4 and "role" in defaults["prompt"]


async def test_field_values_for_dropdowns(app, client):
    _perms(app, "auto_scam.view", "auto_scam.manage")
    raw = json.dumps([
        {"customerCRTId": "1", "STAGE_CODE": "DSCN", "DISPOSITION": "WRONG_NUMBER", "INTEREST_CLASSIFICATION": "COLD", "conversation_log": []},
        {"customerCRTId": "2", "STAGE_CODE": "INTERESTED", "DISPOSITION": "NEUTRAL", "INTEREST_CLASSIFICATION": "WARM", "conversation_log": []},
        {"customerCRTId": "3", "STAGE_CODE": "DSCN", "DISPOSITION": "", "conversation_log": []},
        {"customerCRTId": "4", "conversation_log": []},
    ])
    await _upload(client, "v1", raw)
    await _upload(client, "v2", json.dumps([{"customerCRTId": "5", "STAGE_CODE": "BUSY", "conversation_log": []}]))
    data = _unwrap(await client.get(f"{BASE}/field-values", params=P))
    assert data["sampled"] == 5
    assert data["values"] == {
        "STAGE_CODE": ["BUSY", "DSCN", "INTERESTED"],  # distinct, sorted, across the project's files
        "DISPOSITION": ["NEUTRAL", "WRONG_NUMBER"],
        "INTEREST_CLASSIFICATION": ["COLD", "WARM"],
    }
    assert (await client.get(f"{BASE}/field-values", params={"project_id": str(OTHER)})).json()["code"] == 403
    _perms(app, "auto_scam.manage")
    assert (await client.get(f"{BASE}/field-values", params=P)).json()["code"] == 403
