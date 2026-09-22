import mongomock.database
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from mongomock_motor import AsyncMongoMockClient

from users_service.db.mongodb import init_db
from users_service.db.seed import seed_all

# mongomock's Database.list_collection_names doesn't accept the newer
# pymongo/motor kwargs (nameOnly, authorizedCollections) that Beanie's
# init_beanie passes through. Shim it so the mock accepts and ignores them.
_original_list_collection_names = mongomock.database.Database.list_collection_names


def _list_collection_names_compat(self, filter=None, session=None, **_ignored_kwargs):
    return _original_list_collection_names(self, filter=filter, session=session)


mongomock.database.Database.list_collection_names = _list_collection_names_compat


@pytest_asyncio.fixture
async def client():
    mock_client = AsyncMongoMockClient()
    await init_db(mock_client)
    await seed_all()

    from users_service.app import create_app
    from users_service.core.rate_limit import limiter

    limiter.reset()

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
