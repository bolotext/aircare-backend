import pytest

from common.enums import Role
from tests.factories import auth_header, code, data, error_type, make_org, make_user

pytestmark = pytest.mark.asyncio


async def test_admin_creates_organization(client):
    admin = await make_user(None, Role.ADMIN, "admin-org@example.com")

    resp = await client.post(
        "/api/v1/organizations",
        headers=auth_header(admin),
        json={"name": "Acme Corp", "slug": "acme-corp"},
    )
    assert resp.status_code == 200
    assert code(resp) == 201
    body = data(resp)
    assert body["slug"] == "acme-corp"


async def test_duplicate_slug_is_rejected(client):
    admin = await make_user(None, Role.ADMIN, "admin-org2@example.com")

    payload = {"name": "Beta Inc", "slug": "beta-inc"}
    first = await client.post("/api/v1/organizations", headers=auth_header(admin), json=payload)
    assert first.status_code == 200
    assert code(first) == 201

    second = await client.post("/api/v1/organizations", headers=auth_header(admin), json=payload)
    assert second.status_code == 200
    assert code(second) == 409
    assert error_type(second) == "CONFLICT"


async def test_non_admin_cannot_create_org(client):
    org = await make_org("Delta LLC", "delta-llc")
    supervisor = await make_user(org.id, Role.SUPERVISOR, "not-admin@example.com")
    resp = await client.post(
        "/api/v1/organizations",
        headers=auth_header(supervisor),
        json={"name": "Gamma", "slug": "gamma"},
    )
    assert resp.status_code == 200
    assert code(resp) == 403
