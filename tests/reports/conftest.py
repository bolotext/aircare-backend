import mongomock.database
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from mongomock_motor import AsyncMongoMockClient

from reports_service.db.mongodb import init_db

# See services/users/tests/conftest.py for the full explanation: mongomock's
# Database.list_collection_names doesn't accept the newer pymongo/motor
# kwargs Beanie's init_beanie passes through.
_original_list_collection_names = mongomock.database.Database.list_collection_names


def _list_collection_names_compat(self, filter=None, session=None, **_ignored_kwargs):
    return _original_list_collection_names(self, filter=filter, session=session)


mongomock.database.Database.list_collection_names = _list_collection_names_compat


@pytest_asyncio.fixture
async def app():
    mock_client = AsyncMongoMockClient()
    await init_db(mock_client)

    from app import create_app
    from reports_service.calls.model import Call
    from reports_service.core.deps import ProjectCallSource, resolve_project_source

    application = create_app()
    # Every test's app.lifespan never runs (see the `client` fixture below,
    # which drives the ASGI app directly with no lifespan manager), so
    # app.state.users_http_client is never set — resolve_project_source
    # would otherwise crash making a real HTTP call to a users-service that
    # doesn't exist in this test process. Default every test to the fixed
    # shared `calls` collection with no adapter (matching every existing
    # fixture's make_call(), which is already in the native Call schema); a
    # test exercising per-project db/collection routing or a schema adapter
    # overrides this dependency itself with a different source.
    application.dependency_overrides[resolve_project_source] = lambda: ProjectCallSource(
        collection=Call.get_pymongo_collection(), organization_id="", adapter=None
    )
    yield application


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
