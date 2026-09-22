import pytest

from common.enums import Role
from tests.factories import auth_header, code, data, make_user

pytestmark = pytest.mark.asyncio


async def test_admin_lists_databases_on_the_default_connection(client):
    admin = await make_user(None, Role.ADMIN, "admin-mongo-db@example.com")

    resp = await client.get("/api/v1/mongo/databases", headers=auth_header(admin))
    assert code(resp) == 200
    names = data(resp)
    assert isinstance(names, list)
    assert "aircare" in names
    # Mongo's own internal bookkeeping databases are never valid choices for
    # report_db_name.
    assert "admin" not in names
    assert "local" not in names


async def test_admin_lists_collections_of_a_given_db(client):
    admin = await make_user(None, Role.ADMIN, "admin-mongo-coll@example.com")

    resp = await client.get("/api/v1/mongo/databases/aircare/collections", headers=auth_header(admin))
    assert code(resp) == 200
    names = data(resp)
    assert isinstance(names, list)
    assert "roles" in names
    assert "permissions" in names


async def test_non_admin_cannot_browse_mongo(client):
    supervisor = await make_user(None, Role.SUPERVISOR, "supervisor-mongo@example.com")

    resp = await client.get("/api/v1/mongo/databases", headers=auth_header(supervisor))
    assert code(resp) == 403

    resp = await client.get("/api/v1/mongo/databases/aircare/collections", headers=auth_header(supervisor))
    assert code(resp) == 403
