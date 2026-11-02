from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from reports_service.calls.model import Call
from reports_service.core.config import get_settings
from reports_service.patient_calls.model import PatientCall
from reports_service.patients.model import Patient
from reports_service.reports.model import ReportRecipient, ReportRun
from reports_service.risk_thresholds.model import RiskThreshold
from reports_service.s3_storage.model import S3Config
from reports_service.scenarios.model import ScenarioRun

if not hasattr(AsyncIOMotorClient, "_dummay_append_metadata_patched"):
    # See services/users/src/users_service/db/mongodb.py for the full
    # explanation: motor's async method table predates pymongo's
    # MongoClient.append_metadata, which breaks beanie's init_beanie() under
    # mongomock-motor without this shim.
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
    # returns it afterwards for any per-project dynamic db/collection lookup
    # that needs "the same connection", not a freshly created real one.
    _client = active_client
    await init_beanie(
        database=active_client[settings.mongo_db_name],
        document_models=[Call, ScenarioRun, ReportRun, ReportRecipient, Patient, RiskThreshold, PatientCall, S3Config],
    )


async def close_db() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None
