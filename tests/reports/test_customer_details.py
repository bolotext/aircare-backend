import pytest
from beanie import PydanticObjectId

from reports_service.core.deps import get_current_identity
from reports_service.core.users_client import ProjectConfig
from reports_service.db.mongodb import get_client
from tests.reports.factories import make_identity

pytestmark = pytest.mark.asyncio

PROJECT = PydanticObjectId()
SCHEMA = [
    {"key": "city", "type": "string"},
    {"key": "age", "type": "int"},
    {"key": "vip", "type": "bool"},
    {"key": "balance", "type": "decimal"},
    {"key": "tags", "type": "array"},
    {"key": "legacy", "type": "text"},
    {"key": "language", "type": "string", "options": ["Hindi", "English"]},
    {"key": "tier", "type": "int", "options": ["1", "2"]},
]


@pytest.fixture
def configured(app, monkeypatch):
    app.state.users_http_client = None

    async def fake_config(_client, project_id):
        return ProjectConfig(
            project_id=project_id,
            organization_id="o",
            report_db_name="aircare",
            report_collection_name="calls",
            customer_details_collection_name="customers_test",
            customer_details_fields=SCHEMA,
        )

    monkeypatch.setattr("reports_service.customer_details.router.get_project_config", fake_config)
    app.dependency_overrides[get_current_identity] = lambda: make_identity("ADMIN")
    return app


async def test_crud_round_trip_with_type_validation(configured, client):
    params = {"project_id": str(PROJECT)}

    created = await client.post("/api/v1/customer-details", params=params, json={"fields": {"city": "Pune", "age": 41, "vip": True, "balance": "12.50", "tags": ["a"], "legacy": "x"}})
    assert created.json()["code"] == 201
    record = created.json()["data"]
    assert record["fields"] == {
        "city": "Pune", "age": 41, "vip": True, "balance": "12.50", "tags": ["a"], "legacy": "x",
        "language": None, "tier": None,
    }
    ok = await client.post("/api/v1/customer-details", params=params, json={"fields": {"language": "Hindi", "tier": 2}})
    assert ok.json()["code"] == 201
    off_list = await client.post("/api/v1/customer-details", params=params, json={"fields": {"language": "Klingon"}})
    assert off_list.json()["code"] == 422 and "must be one of" in str(off_list.json()["errors"])
    off_tier = await client.post("/api/v1/customer-details", params=params, json={"fields": {"tier": 3}})
    assert off_tier.json()["code"] == 422
    await get_client()["aircare"]["customers_test"].delete_one({"language": "Hindi"})
    stored = await get_client()["aircare"]["customers_test"].find_one({})
    assert type(stored["age"]) is int and type(stored["balance"]).__name__ == "Decimal128"

    bad = await client.post("/api/v1/customer-details", params=params, json={"fields": {"age": 2**40, "nope": 1}})
    assert bad.json()["code"] == 422
    detail = str(bad.json()["errors"])
    assert "age" in detail and "nope" in detail

    listed = await client.get("/api/v1/customer-details", params={**params, "search": "pun"})
    assert listed.json()["data"]["total"] == 1

    updated = await client.patch(f"/api/v1/customer-details/{record['id']}", params=params, json={"fields": {"age": 42}})
    assert updated.json()["data"]["fields"]["age"] == 42

    await get_client()["aircare"]["customers_test"].update_one({"city": "Pune"}, {"$set": {"internal_note": "n1"}})
    detail = await client.get(f"/api/v1/customer-details/{record['id']}", params=params)
    assert detail.json()["data"]["other_fields"] == {"internal_note": "n1"}
    assert "other_fields" not in str(listed.json()["data"]["items"][0]) or listed.json()["data"]["items"][0]["other_fields"] == {}

    deleted = await client.delete(f"/api/v1/customer-details/{record['id']}", params=params)
    assert deleted.json()["code"] == 200
    missing = await client.get(f"/api/v1/customer-details/{record['id']}", params=params)
    assert missing.json()["code"] == 404

    await get_client()["aircare"]["customers_test"].delete_many({})


async def test_viewer_cannot_read_customer_details(configured, client):
    configured.dependency_overrides[get_current_identity] = lambda: make_identity("VIEWER", project_ids=[str(PROJECT)])
    resp = await client.get("/api/v1/customer-details", params={"project_id": str(PROJECT)})
    assert resp.json()["code"] == 403
