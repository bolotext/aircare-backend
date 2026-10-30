from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from users_service.auth.model import RefreshToken
from users_service.core.config import get_settings
from users_service.organizations.model import Organization
from users_service.permissions.model import Permission, RolePermissionMapping
from users_service.projects.model import Project, UserProjectAssignment
from users_service.roles.model import RoleDoc
from users_service.users.model import User

if not hasattr(AsyncIOMotorClient, "_dummay_append_metadata_patched"):
    # motor's async method table predates pymongo's MongoClient.append_metadata,
    # so unpatched attribute access falls through to motor's "unknown attribute ->
    # treat as a database name" convenience shorthand instead of the real method,
    # which breaks beanie's init_beanie() (it calls database.client.append_metadata(...)).
    # Delegate straight to the underlying (synchronous, non-network) pymongo call.
    def _append_metadata(self: AsyncIOMotorClient, *args, **kwargs) -> None:
        self.delegate.append_metadata(*args, **kwargs)

    AsyncIOMotorClient.append_metadata = _append_metadata
    AsyncIOMotorClient._dummay_append_metadata_patched = True

_client: AsyncIOMotorClient | None = None


def get_client() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        settings = get_settings()
        _client = AsyncIOMotorClient(settings.mongo_uri)
    return _client


async def init_db(client: AsyncIOMotorClient | None = None) -> None:
    global _client
    settings = get_settings()
    active_client = client or get_client()
    # Cache whatever client actually ends up in use (including one passed in
    # explicitly, e.g. mongomock-motor in tests) so get_client() reliably
    # returns it afterwards — see mongo_explorer/router.py, which reuses this
    # connection by default rather than opening a new one.
    _client = active_client
    await init_beanie(
        database=active_client[settings.mongo_db_name],
        document_models=[
            Organization,
            User,
            RefreshToken,
            RoleDoc,
            Permission,
            RolePermissionMapping,
            Project,
            UserProjectAssignment,
        ],
    )


async def close_db() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None
