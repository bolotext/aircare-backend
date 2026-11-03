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
    assert "role" in cfg["prompt"] and set(cfg) == {"prompt", "rules", "field_mapper", "workers", "max_retries"}
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
    assert a["fields"] == [{"field": "INTEREST_CLASSIFICATION", "updated": "HOT"}]  # the analytics JSON as stored
    assert a["mapped"] == [{"label": "Lead score", "origin": "analytics", "key": "INTEREST_CLASSIFICATION", "value": "HOT", "found": True}]
    assert a["changed"] is True  # the model's INTEREST_CLASSIFICATION differs from the conversation's WARM
    assert (a["previous_stage_code"], a["updated_stage_code"]) == ("NEUTRAL", "NEUTRAL")
    listed = _unwrap(await client.get(f"{BASE}/datasets/nov/records", params=P))
    assert listed["columns"] == [{"label": "Lead score", "origin": "analytics", "key": "INTEREST_CLASSIFICATION"}]  # default: one column
    first = listed["items"][0]
    assert first["mapped"] == {"Lead score": "HOT"} and first["analysed"] is True  # ONE column, no before/after pair
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
    r = await client.post(f"{BASE}/datasets/f2/records/bulk-delete", json={**P, "all_matching": True, "filters": {"q": "DSCN"}})
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


async def test_field_mapper_shows_result_keys_and_conversation_fields_as_single_columns(app, client, fake_scam):
    _perms(app, "auto_scam.view", "auto_scam.manage", "auto_scam.run")
    raw = json.dumps([
        {"customerCRTId": "a", "customerName": "Narendra", "user_context": {"city": "Chennai"}, "INTEREST_CLASSIFICATION": "WARM",
         "STAGE_CODE": "INTERESTED", "setupTime": 120, "conversation_log": []},
        {"customerCRTId": "b", "customerName": "Priya", "INTEREST_CLASSIFICATION": "COLD", "STAGE_CODE": "DSCN", "conversation_log": []},
    ])
    await _upload(client, "multi", raw)
    # the model returns the lead score plus a reason (and some nested JSON)
    fake_scam["extra"] = {"REASON": "Only confirmed identity.", "detail": {"score": 3}}
    cfg = _unwrap(await client.get(f"{BASE}/datasets/multi/config", params=P))
    mapper = [
        {"label": "Customer name", "origin": "conversation", "key": "customerName"},
        {"label": "City", "origin": "conversation", "key": "user_context.city"},        # nested conversation field
        {"label": "Lead score", "origin": "analytics", "key": "INTEREST_CLASSIFICATION"},
        {"label": "Reason", "origin": "analytics", "key": "REASON"},
        {"label": "Detail score", "origin": "analytics", "key": "detail.score"},         # nested analytics path
        {"label": "Nope", "origin": "analytics", "key": "not.there"},
    ]
    assert (await client.put(f"{BASE}/datasets/multi/config", params=P, json={**cfg, "field_mapper": mapper})).json()["code"] == 200
    for bad in (
        [mapper[0], {**mapper[1], "label": "Customer name"}],                     # duplicate titles
        [{"label": " ", "origin": "analytics", "key": "x"}],                       # blank title
        [{"label": "x", "origin": "somewhere", "key": "x"}],                       # unknown origin
        [{"label": "x", "origin": "analytics", "key": ""}],                        # blank key
    ):
        assert (await client.put(f"{BASE}/datasets/multi/config", params=P, json={**cfg, "field_mapper": bad})).json()["code"] == 422

    # before any run: conversation columns already have values, analytics columns do not
    pre = _unwrap(await client.get(f"{BASE}/datasets/multi/records", params=P))
    assert [c["label"] for c in pre["columns"]] == [m["label"] for m in mapper]
    first = next(i for i in pre["items"] if i["customerCRTId"] == "a")
    assert first["mapped"] == {"Customer name": "Narendra", "City": "Chennai"} and first["analysed"] is False

    await _run(client, "multi")
    listed = _unwrap(await client.get(f"{BASE}/datasets/multi/records", params=P))
    first = next(i for i in listed["items"] if i["customerCRTId"] == "a")
    assert first["mapped"] == {"Customer name": "Narendra", "City": "Chennai", "Lead score": "HOT",
                               "Reason": "Only confirmed identity.", "Detail score": 3}  # "Nope" is simply absent
    assert first["analysed"] is True

    data = _unwrap(await client.get(f"{BASE}/datasets/multi/results", params=P))
    row = next(r for r in data["items"] if r["customerCRTId"] == "a")
    by = {m["label"]: m for m in row["mapped"]}
    assert by["Customer name"] == {"label": "Customer name", "origin": "conversation", "key": "customerName", "value": "Narendra", "found": True}
    assert by["Reason"]["value"] == "Only confirmed identity." and by["Reason"]["origin"] == "analytics"
    assert by["Nope"]["found"] is False and by["Nope"]["value"] is None
    stored = {f["field"]: f["updated"] for f in row["fields"]}
    assert stored["REASON"] == "Only confirmed identity." and stored["detail"] == {"score": 3}  # the stored JSON keeps every key

    # changing the mapper applies to existing results immediately, with no re-run
    only = [{"label": "Why", "origin": "analytics", "key": "REASON"}]
    await client.put(f"{BASE}/datasets/multi/config", params=P, json={**cfg, "field_mapper": only})
    again = next(r for r in _unwrap(await client.get(f"{BASE}/datasets/multi/results", params=P))["items"] if r["customerCRTId"] == "a")
    assert [(m["label"], m["value"]) for m in again["mapped"]] == [("Why", "Only confirmed identity.")]
    assert len(fake_scam["calls"]) == 2  # only the first run called the model


async def test_changed_only_and_mapper_sources(app, client, fake_scam):
    _perms(app, "auto_scam.view", "auto_scam.manage", "auto_scam.run")
    raw = json.dumps([
        {"customerCRTId": "same", "customerName": "A", "INTEREST_CLASSIFICATION": "HOT", "conversation_log": []},    # model says HOT: unchanged
        {"customerCRTId": "diff", "customerName": "B", "INTEREST_CLASSIFICATION": "COLD", "conversation_log": []},   # -> changed
    ])
    await _upload(client, "flt2", raw)
    fake_scam["extra"] = {"REASON": "price", "detail": {"reason": "cost", "score": 3}}
    await _run(client, "flt2")
    assert sorted(r["customerCRTId"] for r in _unwrap(await client.get(f"{BASE}/datasets/flt2/results", params=P))["items"]) == ["diff", "same"]
    changed = _unwrap(await client.get(f"{BASE}/datasets/flt2/results", params={**P, "changed_only": True}))["items"]
    assert [r["customerCRTId"] for r in changed] == ["diff"]  # REASON has no counterpart on the conversation, so it never counts

    src = _unwrap(await client.get(f"{BASE}/datasets/flt2/mapper-sources", params=P))
    a = {k["path"]: k["sample"] for k in src["analytics"]}
    c = {k["path"]: k["sample"] for k in src["conversation"]}
    assert a["INTEREST_CLASSIFICATION"] == "HOT" and a["REASON"] == "price" and a["detail.reason"] == "cost" and a["detail.score"] == 3
    assert c["customerName"] in ("A", "B") and "INTEREST_CLASSIFICATION" in c and "conversation_log" not in c
    assert not any(k in c for k in ("project_id", "aircare_project_id", "call_date", "uploaded_at", "_id"))  # internals hidden
    _perms(app, "auto_scam.manage")
    assert (await client.get(f"{BASE}/datasets/flt2/mapper-sources", params=P)).json()["code"] == 403


def test_mapper_lookup_and_migration_of_older_settings():
    from reports_service.auto_scam.mapper import analytics_paths, changed_fields, lookup, same, stage_codes
    from reports_service.auto_scam.model import AutoScamConfig

    data = {"a.b": 1, "x": {"y": [10, {"z": "deep"}]}}
    assert lookup(data, "a.b") == (True, 1)  # an exact key containing a dot wins
    assert lookup(data, "x.y.1.z") == (True, "deep") and lookup(data, "x.y.5") == (False, None) and lookup(data, "nope") == (False, None)
    assert {p["path"] for p in analytics_paths([{"k": {"n": 1}}, {"k": {"m": 2}}, {"top": "v"}])} == {"k.n", "k.m", "top"}
    assert same(49, "49") and same(None, None) and not same("a", None)
    assert changed_fields({"INTEREST_CLASSIFICATION": "HOT", "REASON": "r", "X": 1}, {"INTEREST_CLASSIFICATION": "COLD", "X": 1}) == ["INTEREST_CLASSIFICATION"]
    assert stage_codes({"STAGE_CODE": "NEW"}, {"STAGE_CODE": "OLD"}) == ("OLD", "NEW")
    assert stage_codes({}, {"STAGE_CODE": "OLD"}) == ("OLD", "OLD")
    # settings saved by earlier versions keep working
    old_columns = AutoScamConfig(prompt="p", updated_columns=[{"field": "DISPOSITION", "name": "New disposition"}])
    assert [(m.label, m.origin, m.key) for m in old_columns.field_mapper] == [("New disposition", "analytics", "DISPOSITION")]
    old_mapper = AutoScamConfig(prompt="p", field_mapper=[{"source": "mood", "field": "SENTIMENT", "name": "New SENTIMENT"}])
    assert [(m.label, m.origin, m.key) for m in old_mapper.field_mapper] == [("New SENTIMENT", "analytics", "mood")]
    assert [(m.label, m.key) for m in AutoScamConfig(prompt="p").field_mapper] == [("Lead score", "INTEREST_CLASSIFICATION")]


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
    run = await go(all_matching=True, filters={"q": "T"})
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
    cfg["field_mapper"] = [
        {"label": "New stage", "origin": "analytics", "key": "STAGE_CODE"}, {"label": "New disposition", "origin": "analytics", "key": "DISPOSITION"},
        {"label": "Lead score", "origin": "analytics", "key": "INTEREST_CLASSIFICATION"},
    ]
    assert (await client.put(f"{BASE}/datasets/rules/config", params=P, json=cfg)).json()["code"] == 200
    bad = {**cfg, "rules": [{"when": [{"field": "x", "op": "regex", "value": "a"}], "then": [{"field": "y", "value": 1}]}]}
    assert (await client.put(f"{BASE}/datasets/rules/config", params=P, json=bad)).json()["code"] == 422
    assert (await client.put(f"{BASE}/datasets/rules/config", params=P, json={**cfg, "rules": [{"when": [], "then": []}]})).json()["code"] == 422

    run = await _run(client, "rules")
    assert run["status"] == "succeeded"
    result_rows = {r["customerCRTId"]: r for r in _unwrap(await client.get(f"{BASE}/datasets/rules/results", params=P))["items"]}
    stored = {cid: {f["field"]: f["updated"] for f in r["fields"]} for cid, r in result_rows.items()}
    mapped = {cid: {m["label"]: m for m in r["mapped"]} for cid, r in result_rows.items()}
    # fake model says HOT: a (INTERESTED, 10s) -> default rule HOT->WARM, then the new rule sets DISPOSITION
    assert stored["a"]["INTEREST_CLASSIFICATION"] == "WARM" and stored["a"]["DISPOSITION"] == "CALLBACK"
    assert mapped["a"]["New disposition"]["value"] == "CALLBACK"
    # b (DSCN) -> COLD by the default rules; the DISPOSITION rule does not apply, so the JSON has no DISPOSITION
    assert stored["b"]["INTEREST_CLASSIFICATION"] == "COLD" and "DISPOSITION" not in stored["b"]
    assert mapped["b"]["New disposition"]["found"] is False
    assert (result_rows["a"]["previous_stage_code"], result_rows["a"]["updated_stage_code"]) == ("INTERESTED", "CALLBACK_SCHEDULED")
    assert (result_rows["b"]["previous_stage_code"], result_rows["b"]["updated_stage_code"]) == ("DSCN", "DSCN")  # rule didn't apply
    listed = _unwrap(await client.get(f"{BASE}/datasets/rules/records", params=P))
    assert [c["label"] for c in listed["columns"]] == ["New stage", "New disposition", "Lead score"]
    first = next(i for i in listed["items"] if i["customerCRTId"] == "a")
    assert first["mapped"] == {"New stage": "CALLBACK_SCHEDULED", "New disposition": "CALLBACK", "Lead score": "WARM"}
    assert first["STAGE_CODE"] == "INTERESTED"  # the conversation's own stage code is untouched


async def test_rules_dry_run_endpoint(app, client):
    _perms(app, "auto_scam.view")
    body = {"rules": [{"name": "r", "when": [{"field": "STAGE_CODE", "op": "eq", "value": "DSCN"}], "then": [{"field": "DISPOSITION", "value": "DROPPED"}]}],
            "conversation": {"STAGE_CODE": "DSCN"}, "analytics": {"INTEREST_CLASSIFICATION": "HOT"}}
    data = _unwrap(await client.post(f"{BASE}/rules/test", json=body))
    assert data["analytics"] == {"INTEREST_CLASSIFICATION": "HOT", "DISPOSITION": "DROPPED"} and data["fired"] == ["r"]
    assert data["trace"] == [{
        "index": 1, "rule": "r", "match": "all",
        "conditions": [{"field": "STAGE_CODE", "op": "eq", "value": "DSCN", "actual": "DSCN", "met": True}],
        "changes": [{"field": "DISPOSITION", "from": None, "to": "DROPPED"}],
    }]
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


# ------------------------------------------------------------------ filters
def _rows():
    day = lambda d: 1792987551 + d * 86400  # 2026-10-26 + d days (UTC)
    return json.dumps([
        {"customerCRTId": "r1", "tempDate": "26/10/2026", "STAGE_CODE": "DSCN", "DISPOSITION": "WRONG_NUMBER", "INTEREST_CLASSIFICATION": "COLD", "conversation_log": []},
        {"customerCRTId": "r2", "tempDate": "27/10/2026", "STAGE_CODE": "INTERESTED", "DISPOSITION": "NEUTRAL", "INTEREST_CLASSIFICATION": "WARM", "conversation_log": []},
        {"customerCRTId": "r3", "tempDate": "28/10/2026", "STAGE_CODE": "INTERESTED", "DISPOSITION": "NOT_INTERESTED", "INTEREST_CLASSIFICATION": "COLD", "conversation_log": []},
        {"customerCRTId": "r4", "STAGE_CODE": "BUSY", "DISPOSITION": "NEUTRAL", "INTEREST_CLASSIFICATION": "HOT",  # no tempDate: from the first message timestamp
         "conversation_log": [{"role": "assistant", "content": "hi", "timestamp": day(5)}]},
        {"customerCRTId": "r5", "STAGE_CODE": "BUSY", "conversation_log": []},  # no date at all
    ])


def test_call_date_is_derived_from_tempdate_or_first_timestamp():
    from reports_service.auto_scam.records import derive_call_date

    assert derive_call_date({"tempDate": "26/10/2026"}) == "2026-10-26"
    assert derive_call_date({"tempDate": "bad", "conversation_log": [{"timestamp": 1792987551.06}]}) == "2026-10-26"
    assert derive_call_date({"conversation_log": [{"role": "x"}, {"timestamp": 1793073951}]}) == "2026-10-27"
    assert derive_call_date({"tempDate": "", "conversation_log": []}) is None
    assert derive_call_date({"conversation_log": [{"timestamp": True}, "junk"]}) is None


async def test_filters_by_disposition_stage_class_and_date(app, client):
    _perms(app, "auto_scam.view", "auto_scam.manage", "auto_scam.run")
    await _upload(client, "flt", _rows())

    async def ids(**params):
        data = _unwrap(await client.get(f"{BASE}/datasets/flt/records", params={**P, **params}))
        return sorted(i["customerCRTId"] for i in data["items"]), data["total"]

    assert (await ids())[1] == 5
    assert (await ids(dispositions=["NEUTRAL"]))[0] == ["r2", "r4"]
    assert (await ids(dispositions=["NEUTRAL", "WRONG_NUMBER"]))[0] == ["r1", "r2", "r4"]  # multi: any of
    assert (await ids(stage_codes=["INTERESTED"]))[0] == ["r2", "r3"]
    assert (await ids(classifications=["COLD", "HOT"]))[0] == ["r1", "r3", "r4"]
    assert (await ids(stage_codes=["INTERESTED", "BUSY"], dispositions=["NEUTRAL"]))[0] == ["r2", "r4"]  # AND across filters
    assert (await ids(date_from="2026-10-27", date_to="2026-10-28"))[0] == ["r2", "r3"]
    assert (await ids(date_from="2026-10-28"))[0] == ["r3", "r4"]  # r4's date comes from its first timestamp (2026-10-31)
    assert (await ids(date_to="2026-10-26"))[0] == ["r1"]  # undated r5 never matches a date filter
    assert (await ids(q="r2", dispositions=["NEUTRAL"]))[0] == ["r2"]
    assert (await ids(dispositions=["NOPE"]))[0] == []
    items = _unwrap(await client.get(f"{BASE}/datasets/flt/records", params={**P, "stage_codes": ["DSCN"]}))["items"]
    assert items[0]["call_date"] == "2026-10-26"
    bad = await client.get(f"{BASE}/datasets/flt/records", params={**P, "date_from": "26/10/2026"})
    assert bad.json()["code"] == 422
    # the derived date is not part of the editable record, and editing re-derives it
    rid = items[0]["id"]
    rec = _unwrap(await client.get(f"{BASE}/records/{rid}"))
    assert "call_date" not in rec
    rec["tempDate"] = "01/11/2026"
    await client.put(f"{BASE}/records/{rid}", json={**rec, "call_date": "1999-01-01"})
    assert (await ids(date_from="2026-11-01"))[0] == ["r1"]


async def test_filter_options_come_from_the_files_latest_conversations(app, client):
    _perms(app, "auto_scam.view", "auto_scam.manage")
    await _upload(client, "flt", _rows())
    await _upload(client, "other", json.dumps([{"customerCRTId": "o", "STAGE_CODE": "ONLY_IN_OTHER", "tempDate": "01/01/2026", "conversation_log": []}]))
    data = _unwrap(await client.get(f"{BASE}/field-values", params={**P, "file_name": "flt"}))
    assert data["values"]["STAGE_CODE"] == ["BUSY", "DSCN", "INTERESTED"]  # not ONLY_IN_OTHER
    assert data["values"]["DISPOSITION"] == ["NEUTRAL", "NOT_INTERESTED", "WRONG_NUMBER"]
    assert data["dates"] == {"min": "2026-10-26", "max": "2026-10-31"}
    whole = _unwrap(await client.get(f"{BASE}/field-values", params=P))
    assert "ONLY_IN_OTHER" in whole["values"]["STAGE_CODE"] and whole["dates"]["min"] == "2026-01-01"


async def test_bulk_actions_use_the_same_filters(app, client, fake_scam):
    _perms(app, "auto_scam.view", "auto_scam.manage", "auto_scam.run")
    await _upload(client, "flt", _rows())
    filt = {"dispositions": ["NEUTRAL"], "date_from": "2026-10-27"}
    started = _unwrap(await client.post(f"{BASE}/runs", json={**P, "file_names": ["flt"], "all_matching": True, "filters": filt}))
    run_id = started["started"][0]["run_id"]
    for _ in range(100):
        run = _unwrap(await client.get(f"{BASE}/runs/{run_id}"))
        if run["status"] not in ("queued", "running"):
            break
        await asyncio.sleep(0.1)
    assert run["status"] == "succeeded" and run["filtered"] is True and "matching the filters" in run["log"]
    assert sorted(c for c, _ in fake_scam["calls"]) == ["r2", "r4"]

    r = await client.post(f"{BASE}/datasets/flt/records/bulk-delete", json={**P, "all_matching": True, "filters": {"stage_codes": ["BUSY"], "dispositions": ["NEUTRAL"]}})
    assert _unwrap(r)["deleted"] == 1  # only r4
    left = _unwrap(await client.get(f"{BASE}/datasets/flt/records", params=P))
    assert sorted(i["customerCRTId"] for i in left["items"]) == ["r1", "r2", "r3", "r5"]


async def test_startup_backfills_call_date_for_older_records(app, client):
    from reports_service.auto_scam.records import ensure_indexes

    await records_collection().insert_one({"aircare_project_id": PROJECT, "project_id": "old", "customerCRTId": "x", "tempDate": "05/06/2026"})
    await ensure_indexes()
    assert (await records_collection().find_one({"customerCRTId": "x"}))["call_date"] == "2026-06-05"


# ------------------------------------------------------------------ chunked big-file upload
@pytest.fixture()
def upload_dir(tmp_path, monkeypatch):
    from reports_service.auto_scam import uploads

    monkeypatch.setattr(uploads, "UPLOAD_DIR", tmp_path / "uploads")
    return tmp_path / "uploads"


async def _chunked(client, name, payload: bytes, chunk=7, overwrite=False, finish=True):
    """Upload `payload` in tiny chunks (so boundaries fall mid-object and mid-character) and wait for processing."""
    start = await client.post(f"{BASE}/uploads", json={**P, "file_name": name, "size": len(payload), "overwrite": overwrite})
    if start.json()["code"] != 201:
        return start.json(), None
    uid = _unwrap(start)["upload_id"]
    for off in range(0, len(payload), chunk):
        r = await client.put(f"{BASE}/uploads/{uid}/chunk", params={"offset": off}, content=payload[off : off + chunk],
                             headers={"Content-Type": "application/octet-stream"})
        assert r.json()["code"] == 200, r.json()
    if not finish:
        return None, uid
    assert (await client.post(f"{BASE}/uploads/{uid}/finish")).json()["code"] == 202
    for _ in range(300):
        st = _unwrap(await client.get(f"{BASE}/uploads/{uid}"))
        if st["status"] not in ("receiving", "processing"):
            return st, uid
        await asyncio.sleep(0.05)
    raise AssertionError(st)


async def test_chunked_upload_parses_across_chunk_boundaries(app, client, upload_dir):
    from reports_service.auto_scam.uploads import _Parser  # noqa: F401

    _perms(app, "auto_scam.view", "auto_scam.manage")
    payload = SAMPLE.encode("utf-8")  # Tamil text: 3-byte characters get split by the 7-byte chunks
    st, uid = await _chunked(client, "big", payload, chunk=7)
    assert st["status"] == "done" and st["records"] == 1 and st["received"] == st["size"] == len(payload)
    assert st["processed_bytes"] == len(payload)
    rec_id = _unwrap(await client.get(f"{BASE}/datasets/big/records", params=P))["items"][0]["id"]
    rec = _unwrap(await client.get(f"{BASE}/records/{rec_id}"))
    assert rec["conversation_log"][1]["content"] == "ஆம்" and rec["source_id"] == "6ae8b6994f6ebabc318ed040"
    assert rec["customerCRTId"] == "47c7c1ac-a100-4396-8953-a052ced333ae"
    ds = _unwrap(await client.get(f"{BASE}/datasets", params=P))
    assert [(d["file_name"], d["records"]) for d in ds] == [("big", 1)]
    assert not (upload_dir / f"{uid}.part").exists()  # temp file cleaned up
    assert await records_collection().count_documents({"project_id": {"$regex": "^__upload__"}}) == 0


async def test_chunked_upload_handles_many_records_jsonl_and_single_object(app, client, upload_dir):
    _perms(app, "auto_scam.view", "auto_scam.manage")
    many = json.dumps([{"customerCRTId": f"c{i}", "STAGE_CODE": "S", "tempDate": "26/10/2026", "conversation_log": [{"role": "user", "content": "é"}]}
                       for i in range(2500)], ensure_ascii=False, indent=1).encode()
    st, _ = await _chunked(client, "many", many, chunk=4096)  # > one insert batch, many block boundaries
    assert st["status"] == "done" and st["records"] == 2500
    assert _unwrap(await client.get(f"{BASE}/datasets/many/records", params={**P, "date_from": "2026-10-26"}))["total"] == 2500

    lines = "\n".join(json.dumps({"customerCRTId": f"l{i}", "conversation_log": []}) for i in range(5)).encode()
    st, _ = await _chunked(client, "lines", lines, chunk=13)
    assert (st["status"], st["records"]) == ("done", 5)
    one = json.dumps({"customerCRTId": "solo", "conversation_log": []}).encode()
    st, _ = await _chunked(client, "solo", one, chunk=5)
    assert (st["status"], st["records"]) == ("done", 1)


async def test_chunked_upload_rejects_bad_files_and_keeps_existing_data(app, client, upload_dir):
    _perms(app, "auto_scam.view", "auto_scam.manage")
    good = json.dumps([{"customerCRTId": "keep", "conversation_log": []}]).encode()
    assert (await _chunked(client, "ds", good))[0]["status"] == "done"

    for label, bad in [
        ("truncated", b'[{"customerCRTId": "a", "conversation_log": []}, {"customerCRTId": "b"'),
        ("not objects", b"[1, 2, 3]"),
        ("empty array", b"[]"),
        ("garbage", b"{not json at all"),
        ("bad utf8", b'[{"customerCRTId": "\xff\xfe"}]'),
    ]:
        st, uid = await _chunked(client, "ds", bad, overwrite=True)
        assert st["status"] == "failed" and st["error"], label
        assert not (upload_dir / f"{uid}.part").exists(), label
        # the previous dataset is untouched and no staging rows remain
        assert [i["customerCRTId"] for i in _unwrap(await client.get(f"{BASE}/datasets/ds/records", params=P))["items"]] == ["keep"], label
        assert await records_collection().count_documents({"project_id": {"$regex": "^__upload__"}}) == 0, label

    # overwrite replaces it only when the new file is good
    new = json.dumps([{"customerCRTId": "new1", "conversation_log": []}, {"customerCRTId": "new2", "conversation_log": []}]).encode()
    st, _ = await _chunked(client, "ds", new, overwrite=True)
    assert st["status"] == "done"
    assert sorted(i["customerCRTId"] for i in _unwrap(await client.get(f"{BASE}/datasets/ds/records", params=P))["items"]) == ["new1", "new2"]


async def test_chunked_upload_protocol_guards(app, client, upload_dir):
    _perms(app, "auto_scam.view", "auto_scam.manage")
    payload = json.dumps([{"customerCRTId": "a", "conversation_log": []}]).encode()
    await _chunked(client, "exists", payload)
    # a duplicate name is refused up front, before any data is sent
    refused, _ = await _chunked(client, "exists", payload)
    assert refused["code"] == 409
    assert (await client.post(f"{BASE}/uploads", json={**P, "file_name": "../x", "size": 10})).json()["code"] == 400
    assert (await client.post(f"{BASE}/uploads", json={**P, "file_name": "huge", "size": 10 * 1024**3})).json()["code"] == 400

    start = _unwrap(await client.post(f"{BASE}/uploads", json={**P, "file_name": "g", "size": len(payload)}))
    uid = start["upload_id"]
    hdr = {"Content-Type": "application/octet-stream"}
    assert (await client.post(f"{BASE}/uploads", json={**P, "file_name": "g", "size": 5})).json()["code"] == 409  # already uploading
    gap = await client.put(f"{BASE}/uploads/{uid}/chunk", params={"offset": 5}, content=payload[5:], headers=hdr)
    assert gap.json()["code"] == 409  # chunk would leave a hole
    over = await client.put(f"{BASE}/uploads/{uid}/chunk", params={"offset": 0}, content=payload + b"x", headers=hdr)
    assert over.json()["code"] == 400
    assert (await client.post(f"{BASE}/uploads/{uid}/finish")).json()["code"] == 409  # nothing received yet
    half = len(payload) // 2
    await client.put(f"{BASE}/uploads/{uid}/chunk", params={"offset": 0}, content=payload[:half], headers=hdr)
    await client.put(f"{BASE}/uploads/{uid}/chunk", params={"offset": 0}, content=payload[:half], headers=hdr)  # retry is safe
    assert _unwrap(await client.get(f"{BASE}/uploads/{uid}"))["received"] == half
    assert (await client.post(f"{BASE}/uploads/{uid}/finish")).json()["code"] == 409  # incomplete
    # cancel discards everything
    assert (await client.delete(f"{BASE}/uploads/{uid}")).json()["code"] == 200
    assert _unwrap(await client.get(f"{BASE}/uploads/{uid}"))["status"] == "cancelled"
    assert not any(upload_dir.glob("*.part")) or all(p.stem != uid for p in upload_dir.glob("*.part"))
    assert (await client.put(f"{BASE}/uploads/{uid}/chunk", params={"offset": half}, content=payload[half:], headers=hdr)).json()["code"] == 409
    # permissions + project isolation
    _perms(app, "auto_scam.view")
    assert (await client.post(f"{BASE}/uploads", json={**P, "file_name": "z", "size": 10})).json()["code"] == 403


async def test_stale_uploads_are_swept(app, client, upload_dir):
    from datetime import timedelta

    from reports_service.auto_scam.model import AutoScamUpload, utcnow
    from reports_service.auto_scam.uploads import sweep_uploads

    _perms(app, "auto_scam.view", "auto_scam.manage")
    st, uid = await _chunked(client, "abandoned", b'[{"customerCRTId": "a", "conversation_log": []}]', finish=False)
    up = await AutoScamUpload.get(PydanticObjectId(uid))
    await sweep_uploads()
    assert (await AutoScamUpload.get(up.id)).status == "receiving"  # fresh: left alone
    up.heartbeat_at = utcnow() - timedelta(hours=7)
    await up.save()
    await sweep_uploads()
    swept = await AutoScamUpload.get(up.id)
    assert swept.status == "failed" and not (upload_dir / f"{uid}.part").exists()


async def test_bulk_delete_results_keeps_conversations_and_they_are_reanalysed(app, client, fake_scam):
    _perms(app, "auto_scam.view", "auto_scam.manage", "auto_scam.run")
    await _upload(client, "res", RAW)  # customerCRTIds a, b
    await _upload(client, "res2", RAW)
    await _run(client, "res")
    await _run(client, "res2")
    rows = _unwrap(await client.get(f"{BASE}/datasets/res/results", params=P))["items"]
    other = _unwrap(await client.get(f"{BASE}/datasets/res2/results", params=P))["items"][0]["id"]
    a = next(r["id"] for r in rows if r["customerCRTId"] == "a")

    # selected ids only; an id from another file is ignored
    r = await client.post(f"{BASE}/datasets/res/results/bulk-delete", json={**P, "result_ids": [a, other]})
    assert _unwrap(r)["deleted"] == 1
    left = _unwrap(await client.get(f"{BASE}/datasets/res/results", params=P))["items"]
    assert [x["customerCRTId"] for x in left] == ["b"]
    assert len(_unwrap(await client.get(f"{BASE}/datasets/res2/results", params=P))["items"]) == 2  # other file untouched
    # the conversations are still there, and the deleted one shows as not analysed
    listed = _unwrap(await client.get(f"{BASE}/datasets/res/records", params=P))
    assert listed["total"] == 2
    by = {i["customerCRTId"]: i for i in listed["items"]}
    assert by["a"]["analysed"] is False and by["b"]["analysed"] is True
    counts = {d["file_name"]: d["analysed"] for d in _unwrap(await client.get(f"{BASE}/datasets", params=P))}
    assert counts == {"res": 1, "res2": 2}  # the files list follows the results

    # the next run analyses exactly the conversation whose result was deleted
    fake_scam["calls"].clear()
    run = await _run(client, "res")
    assert (run["total"], run["skipped"]) == (1, 1) and [c for c, _ in fake_scam["calls"]] == ["a"]

    assert (await client.post(f"{BASE}/datasets/res/results/bulk-delete", json={**P, "result_ids": []})).json()["code"] == 422
    assert (await client.post(f"{BASE}/datasets/res/results/bulk-delete", json={**P, "result_ids": ["nope"]})).json()["code"] == 400
    _perms(app, "auto_scam.view")
    assert (await client.post(f"{BASE}/datasets/res/results/bulk-delete", json={**P, "result_ids": [a]})).json()["code"] == 403


async def test_field_mapper_order_is_kept_and_drives_the_column_order(app, client, fake_scam):
    _perms(app, "auto_scam.view", "auto_scam.manage", "auto_scam.run")
    await _upload(client, "ord", json.dumps([{"customerCRTId": "a", "customerName": "N", "conversation_log": []}]))
    fake_scam["extra"] = {"REASON": "r"}
    await _run(client, "ord")
    cfg = _unwrap(await client.get(f"{BASE}/datasets/ord/config", params=P))
    cols = [
        {"label": "Reason", "origin": "analytics", "key": "REASON"},
        {"label": "Customer name", "origin": "conversation", "key": "customerName"},
        {"label": "Lead score", "origin": "analytics", "key": "INTEREST_CLASSIFICATION"},
    ]
    for order in (cols, [cols[2], cols[0], cols[1]], list(reversed(cols))):  # move rows up/down: the saved order wins
        assert (await client.put(f"{BASE}/datasets/ord/config", params=P, json={**cfg, "field_mapper": order})).json()["code"] == 200
        assert [c["label"] for c in _unwrap(await client.get(f"{BASE}/datasets/ord/config", params=P))["field_mapper"]] == [c["label"] for c in order]
        assert [c["label"] for c in _unwrap(await client.get(f"{BASE}/datasets/ord/records", params=P))["columns"]] == [c["label"] for c in order]
        res = _unwrap(await client.get(f"{BASE}/datasets/ord/results", params=P))
        assert [c["label"] for c in res["columns"]] == [c["label"] for c in order]
        assert [m["label"] for m in res["items"][0]["mapped"]] == [c["label"] for c in order]


def test_rule_trace_explains_what_changed_and_why():
    from reports_service.auto_scam.rules import Action, Condition, Rule, apply_rules_traced, default_rules, run_rules

    # the default rule "interested, short call: HOT -> WARM", exactly as in the user's setup
    out, trace = apply_rules_traced(default_rules(), {"STAGE_CODE": "INTERESTED", "setupTime": 20}, {"INTEREST_CLASSIFICATION": "HOT"})
    assert out == {"INTEREST_CLASSIFICATION": "WARM"}
    assert len(trace) == 1 and trace[0]["index"] == 2
    assert trace[0]["changes"] == [{"field": "INTEREST_CLASSIFICATION", "from": "HOT", "to": "WARM"}]
    assert [(c["field"], c["op"], c["value"], c["actual"], c["met"]) for c in trace[0]["conditions"]] == [
        ("setupTime", "lt", 35, 20, True), ("STAGE_CODE", "eq", "INTERESTED", "INTERESTED", True),
        ("INTEREST_CLASSIFICATION", "eq", "HOT", "HOT", True),
    ]
    # nothing applies -> empty trace (the model's answer stands)
    assert apply_rules_traced(default_rules(), {"STAGE_CODE": "NEUTRAL", "setupTime": 90}, {"INTEREST_CLASSIFICATION": "HOT"})[1] == []
    # an invalid / missing score is replaced, and the trace says what it was
    _, t = apply_rules_traced(default_rules(), {"STAGE_CODE": "NEUTRAL"}, {"INTEREST_CLASSIFICATION": "bogus"})
    assert t[0]["changes"] == [{"field": "INTEREST_CLASSIFICATION", "from": "bogus", "to": "COLD"}]
    _, t = apply_rules_traced(default_rules(), {"STAGE_CODE": "NEUTRAL"}, {})
    assert t[0]["changes"] == [{"field": "INTEREST_CLASSIFICATION", "from": None, "to": "COLD"}]

    # a rule that fires but sets a value the field already has changed nothing: fired, but not in the trace
    same = Rule(name="noop", when=[Condition(field="STAGE_CODE", op="eq", value="DSCN")], then=[Action(field="INTEREST_CLASSIFICATION", value="COLD")])
    out, fired, trace = run_rules([same], {"STAGE_CODE": "DSCN"}, {"INTEREST_CLASSIFICATION": "COLD"})
    assert fired == ["noop"] and trace == []

    # "any" mode shows which conditions were met; a later rule sees (and is explained by) an earlier rule's change
    r1 = Rule(name="first", match="any", when=[Condition(field="STAGE_CODE", op="eq", value="BUSY"), Condition(field="setupTime", op="lt", value=10)],
              then=[Action(field="DISPOSITION", value="RETRY")])
    r2 = Rule(name="second", when=[Condition(field="DISPOSITION", op="eq", value="RETRY")], then=[Action(field="STAGE_CODE", value="REDIAL")])
    _, trace = apply_rules_traced([r1, r2], {"STAGE_CODE": "BUSY", "setupTime": 50}, {})
    assert [e["rule"] for e in trace] == ["first", "second"]
    assert [c["met"] for c in trace[0]["conditions"]] == [True, False] and trace[0]["match"] == "any"
    assert trace[1]["conditions"][0]["actual"] == "RETRY"  # saw the value the first rule just set
    # a rule with no name is labelled by its position, a disabled rule never shows up
    unnamed = Rule(when=[], then=[Action(field="X", value="1")])
    off = Rule(name="off", enabled=False, when=[], then=[Action(field="Y", value="1")])
    assert [e["rule"] for e in apply_rules_traced([off, unnamed], {}, {})[1]] == ["Rule 2"]


async def test_results_keep_the_model_output_and_the_rule_trace(app, client, fake_scam):
    _perms(app, "auto_scam.view", "auto_scam.manage", "auto_scam.run")
    raw = json.dumps([
        {"customerCRTId": "changed", "STAGE_CODE": "INTERESTED", "setupTime": 10, "INTEREST_CLASSIFICATION": "WARM", "conversation_log": []},
        {"customerCRTId": "stands", "STAGE_CODE": "NEUTRAL", "setupTime": 90, "INTEREST_CLASSIFICATION": "WARM", "conversation_log": []},
        {"customerCRTId": "dscn", "STAGE_CODE": "DSCN", "setupTime": 90, "INTEREST_CLASSIFICATION": "WARM", "conversation_log": []},
    ])
    await _upload(client, "trace", raw)
    fake_scam["extra"] = {"REASON": "because"}  # the fake model always answers HOT + a reason
    await _run(client, "trace")

    rows = {r["customerCRTId"]: r for r in _unwrap(await client.get(f"{BASE}/datasets/trace/results", params=P))["items"]}
    ch, st, ds = rows["changed"], rows["stands"], rows["dscn"]
    # what the model said, before the rules
    assert {f["field"]: f["value"] for f in ch["llm_fields"]} == {"INTEREST_CLASSIFICATION": "HOT", "REASON": "because"}
    # the final value after the rules, and why it differs
    assert {f["field"]: f["updated"] for f in ch["fields"]}["INTEREST_CLASSIFICATION"] == "WARM"
    assert ch["rule_changed"] is True and ch["rules_recorded"] is True
    assert ch["rule_trace"][0]["changes"] == [{"field": "INTEREST_CLASSIFICATION", "from": "HOT", "to": "WARM"}]
    assert all(c["met"] for c in ch["rule_trace"][0]["conditions"]) and ch["rule_trace"][0]["conditions"][0]["actual"] == 10
    # the model's answer stood: recorded, but no rule changed it
    assert st["rule_changed"] is False and st["rules_recorded"] is True and st["rule_trace"] == []
    assert {f["field"]: f["updated"] for f in st["fields"]}["INTEREST_CLASSIFICATION"] == "HOT"
    # another rule: DSCN forces COLD
    assert ds["rule_trace"][0]["changes"] == [{"field": "INTEREST_CLASSIFICATION", "from": "HOT", "to": "COLD"}]
    assert ds["rule_trace"][0]["conditions"][0]["actual"] == "DSCN"

    listed = {i["customerCRTId"]: i for i in _unwrap(await client.get(f"{BASE}/datasets/trace/records", params=P))["items"]}
    assert (listed["changed"]["rule_changed"], listed["stands"]["rule_changed"], listed["dscn"]["rule_changed"]) == (True, False, True)

    # a result saved before the trace existed is reported as "not recorded", not as "no rule applied"
    from reports_service.auto_scam.model import AutoScamResult
    legacy = await AutoScamResult.find_one({"customer_crt_id": "stands"})
    legacy.llm_fields, legacy.rule_trace = [], []
    await legacy.save()
    old = {r["customerCRTId"]: r for r in _unwrap(await client.get(f"{BASE}/datasets/trace/results", params=P))["items"]}["stands"]
    assert old["rules_recorded"] is False and old["rule_trace"] == []


async def test_field_mapper_can_show_the_model_output_and_what_the_rules_changed(app, client, fake_scam):
    _perms(app, "auto_scam.view", "auto_scam.manage", "auto_scam.run")
    raw = json.dumps([
        {"customerCRTId": "changed", "STAGE_CODE": "INTERESTED", "setupTime": 20, "customerName": "Asha", "INTEREST_CLASSIFICATION": "WARM", "conversation_log": []},
        {"customerCRTId": "stands", "STAGE_CODE": "NEUTRAL", "setupTime": 90, "customerName": "Ravi", "INTEREST_CLASSIFICATION": "WARM", "conversation_log": []},
    ])
    await _upload(client, "rc", raw)
    fake_scam["extra"] = {"REASON": "because"}  # the model always says HOT + a reason
    cfg = _unwrap(await client.get(f"{BASE}/datasets/rc/config", params=P))
    mapper = [
        {"label": "Customer", "origin": "conversation", "key": "customerName"},
        {"label": "Final score", "origin": "analytics", "key": "INTEREST_CLASSIFICATION"},
        {"label": "Model said", "origin": "model", "key": "INTEREST_CLASSIFICATION"},
        {"label": "Model reason", "origin": "model", "key": "REASON"},
        {"label": "Changed by rules", "origin": "rules", "key": "changes"},
        {"label": "Rules", "origin": "rules", "key": "rules"},
        {"label": "Because", "origin": "rules", "key": "conditions"},
        {"label": "Rule changed?", "origin": "rules", "key": "changed"},
    ]
    assert (await client.put(f"{BASE}/datasets/rc/config", params=P, json={**cfg, "field_mapper": mapper})).json()["code"] == 200
    for bad in ({"label": "x", "origin": "rules", "key": "nonsense"}, {"label": "x", "origin": "rules", "key": "REASON"}, {"label": "x", "origin": "elsewhere", "key": "a"}):
        assert (await client.put(f"{BASE}/datasets/rc/config", params=P, json={**cfg, "field_mapper": [bad]})).json()["code"] == 422

    # before any run only the conversation column has a value (history does not exist yet)
    pre = {i["customerCRTId"]: i for i in _unwrap(await client.get(f"{BASE}/datasets/rc/records", params=P))["items"]}
    assert pre["changed"]["mapped"] == {"Customer": "Asha"}

    await _run(client, "rc")
    items = {i["customerCRTId"]: i for i in _unwrap(await client.get(f"{BASE}/datasets/rc/records", params=P))["items"]}
    ch = items["changed"]["mapped"]
    assert ch["Customer"] == "Asha" and ch["Final score"] == "WARM"
    assert ch["Model said"] == "HOT" and ch["Model reason"] == "because"          # what the model said, before the rules
    assert ch["Changed by rules"] == "INTEREST_CLASSIFICATION: HOT → WARM"    # the change
    assert ch["Rules"] == "Interested, but call shorter than 35s: HOT -> WARM"
    assert ch["Because"] == "setupTime < 35 (was 20); STAGE_CODE equals INTERESTED (was INTERESTED); INTEREST_CLASSIFICATION equals HOT (was HOT)"
    assert ch["Rule changed?"] == "Yes"
    # the model's answer stood
    st = items["stands"]["mapped"]
    assert st["Final score"] == "HOT" and st["Model said"] == "HOT"
    assert st["Changed by rules"] == "No rule applied" and st["Rule changed?"] == "No" and st["Because"] == "" and st["Rules"] == ""

    # the Results tab gets the same columns
    res = _unwrap(await client.get(f"{BASE}/datasets/rc/results", params=P))
    assert [c["label"] for c in res["columns"]] == [m["label"] for m in mapper]
    row = next(r for r in res["items"] if r["customerCRTId"] == "changed")
    by = {m["label"]: m for m in row["mapped"]}
    assert by["Changed by rules"]["value"] == "INTEREST_CLASSIFICATION: HOT → WARM" and by["Changed by rules"]["origin"] == "rules"
    assert by["Model said"]["value"] == "HOT" and by["Model said"]["found"] is True

    # the mapper's sources list the model keys and the four rule values
    src = _unwrap(await client.get(f"{BASE}/datasets/rc/mapper-sources", params=P))
    assert {k["path"] for k in src["model"]} == {"INTEREST_CLASSIFICATION", "REASON"}
    assert [k["path"] for k in src["rules"]] == ["changes", "rules", "conditions", "changed"]

    # a result saved before the history was kept shows nothing for these columns (not "No rule applied")
    from reports_service.auto_scam.model import AutoScamResult
    legacy = await AutoScamResult.find_one({"customer_crt_id": "stands"})
    legacy.llm_fields, legacy.rule_trace = [], []
    await legacy.save()
    old = {i["customerCRTId"]: i for i in _unwrap(await client.get(f"{BASE}/datasets/rc/records", params=P))["items"]}["stands"]["mapped"]
    assert "Changed by rules" not in old and "Model said" not in old and old["Final score"] == "HOT" and old["Customer"] == "Ravi"


async def test_record_rows_for_csv_export(app, client, fake_scam, monkeypatch):
    from reports_service.auto_scam import router as router_mod

    _perms(app, "auto_scam.view", "auto_scam.manage", "auto_scam.run")
    raw = json.dumps([
        {"customerCRTId": f"c{i}", "customerName": f"Name {i}", "STAGE_CODE": "S" if i < 3 else "T", "DISPOSITION": "D",
         "tempDate": "26/10/2026", "conversation_log": [{"role": "user", "content": "hi"}]} for i in range(6)
    ])
    await _upload(client, "csv", raw)
    await _upload(client, "csv2", raw)
    cfg = _unwrap(await client.get(f"{BASE}/datasets/csv/config", params=P))
    mapper = [{"label": "Customer", "origin": "conversation", "key": "customerName"}, {"label": "Lead score", "origin": "analytics", "key": "INTEREST_CLASSIFICATION"}]
    await client.put(f"{BASE}/datasets/csv/config", params=P, json={**cfg, "field_mapper": mapper})
    # analyse only c1 so some rows have results and some do not
    ids = {i["customerCRTId"]: i["id"] for i in _unwrap(await client.get(f"{BASE}/datasets/csv/records", params=P))["items"]}
    other = _unwrap(await client.get(f"{BASE}/datasets/csv2/records", params=P))["items"][0]["id"]
    started = _unwrap(await client.post(f"{BASE}/runs", json={**P, "file_names": ["csv"], "record_ids": [ids["c1"]]}))["started"][0]["run_id"]
    for _ in range(100):
        if _unwrap(await client.get(f"{BASE}/runs/{started}"))["status"] not in ("queued", "running"):
            break
        await asyncio.sleep(0.1)

    async def rows(**body):
        r = await client.post(f"{BASE}/datasets/csv/records/rows", json={**P, **body})
        return _unwrap(r) if r.json()["code"] == 200 else r.json()

    # selected ids (an id from another file is ignored); same row shape as the table
    sel = await rows(record_ids=[ids["c4"], ids["c1"], other])
    assert [i["customerCRTId"] for i in sel["items"]] == ["c1", "c4"] and sel["truncated"] is False  # file order
    assert [c["label"] for c in sel["columns"]] == ["Customer", "Lead score"]
    by = {i["customerCRTId"]: i for i in sel["items"]}
    assert by["c1"]["mapped"] == {"Customer": "Name 1", "Lead score": "HOT"} and by["c1"]["analysed"] is True
    assert by["c4"]["mapped"] == {"Customer": "Name 4"} and by["c4"]["analysed"] is False  # not analysed: only the conversation column
    assert by["c1"]["call_date"] == "2026-10-26" and by["c1"]["messages"] == 1 and by["c1"]["STAGE_CODE"] == "S"
    # every match of the filters, across pages
    allm = await rows(all_matching=True, filters={"stage_codes": ["T"]})
    assert [i["customerCRTId"] for i in allm["items"]] == ["c3", "c4", "c5"]
    assert len((await rows(all_matching=True))["items"]) == 6
    # nothing selected / bad id / no access
    assert (await rows(record_ids=[]))["code"] == 400 and (await rows(record_ids=["nope"]))["code"] == 400
    # a cap with a flag, so a huge export can never exhaust memory
    monkeypatch.setattr(router_mod, "MAX_TABLE_ROWS", 4)
    capped = await rows(all_matching=True)
    assert len(capped["items"]) == 4 and capped["truncated"] is True
    monkeypatch.setattr(router_mod, "MAX_TABLE_ROWS", 6)
    exact = await rows(all_matching=True)
    assert len(exact["items"]) == 6 and exact["truncated"] is False  # exactly at the cap is not truncated
    _perms(app, "auto_scam.manage")
    assert (await rows(record_ids=[ids["c1"]]))["code"] == 403
