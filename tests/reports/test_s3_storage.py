import io
from datetime import datetime, timezone

import pytest
from beanie import PydanticObjectId
from botocore.exceptions import ClientError
from cryptography.fernet import Fernet

from reports_service.core.config import get_settings
from reports_service.core.deps import get_current_identity
from reports_service.s3_storage import client as s3_client
from reports_service.s3_storage.model import S3Config
from tests.reports.factories import make_identity

pytestmark = pytest.mark.asyncio

PROJECT = PydanticObjectId()
BODY = {"access_key_id": "AKIAEXAMPLE1234", "secret_access_key": "s3cr3t/value", "region_name": "ap-south-1", "bucket_name": "my-bucket"}


class FakeS3:
    def __init__(self, fail_head=None):
        self.fail_head = fail_head

    def head_bucket(self, Bucket):
        if self.fail_head:
            raise ClientError({"Error": {"Code": self.fail_head}}, "HeadBucket")

    def list_objects_v2(self, **kw):
        return {
            "CommonPrefixes": [{"Prefix": "logs/"}],
            "Contents": [
                {"Key": "", "Size": 0},
                {"Key": "a.json", "Size": 7, "LastModified": datetime(2026, 1, 1, tzinfo=timezone.utc), "StorageClass": "STANDARD"},
            ],
            "IsTruncated": True,
            "NextContinuationToken": "tok",
        }

    def get_object(self, Bucket, Key, Range):
        return {"Body": io.BytesIO(b'{"a": 1}'), "ContentType": "application/json", "ContentRange": "bytes 0-7/8"}

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://signed.example/{Params['Key']}?e={ExpiresIn}"


@pytest.fixture(autouse=True)
def _key_and_fake(monkeypatch):
    monkeypatch.setenv("S3_CREDENTIALS_ENCRYPTION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    holder = {"fail": None, "seen": []}

    def factory(access, secret, region):
        holder["seen"].append((access, secret, region))
        return FakeS3(holder["fail"])

    monkeypatch.setattr(s3_client, "make_s3_client", factory)
    yield holder
    get_settings.cache_clear()


def _as(app, role):
    app.dependency_overrides[get_current_identity] = lambda: make_identity(role, project_ids=[str(PROJECT)])


async def test_save_encrypts_never_returns_secret_and_browses(app, client):
    _as(app, "ADMIN")
    params = {"project_id": str(PROJECT)}

    saved = await client.put("/api/v1/s3/config", params=params, json=BODY)
    data = saved.json()["data"]
    assert data["configured"] and data["access_key_hint"] == "1234" and data["bucket_name"] == "my-bucket"
    assert "s3cr3t" not in saved.text and "AKIAEXAMPLE" not in saved.text

    stored = await S3Config.find_one(S3Config.project_id == PROJECT)
    assert stored.secret_access_key_enc.startswith("enc:") and "s3cr3t" not in stored.secret_access_key_enc

    got = await client.get("/api/v1/s3/config", params=params)
    assert "s3cr3t" not in got.text

    listing = (await client.get("/api/v1/s3/objects", params={**params, "prefix": ""})).json()["data"]
    assert listing["prefixes"] == ["logs/"] and [o["key"] for o in listing["objects"]] == ["a.json"]
    assert listing["next_token"] == "tok"

    content = (await client.get("/api/v1/s3/objects/content", params={**params, "key": "a.json"})).json()["data"]
    assert content["content"] == '{"a": 1}' and content["truncated"] is False

    url = (await client.get("/api/v1/s3/objects/download-url", params={**params, "key": "a.json"})).json()["data"]
    assert url["url"].startswith("https://signed.example/a.json") and url["expires_in_seconds"] == 300


async def test_update_with_blank_secret_keeps_saved_credentials(app, client, _key_and_fake):
    _as(app, "ADMIN")
    params = {"project_id": str(PROJECT)}
    await client.put("/api/v1/s3/config", params=params, json=BODY)
    resp = await client.put(
        "/api/v1/s3/config", params=params, json={**BODY, "access_key_id": "", "secret_access_key": "", "bucket_name": "other-bucket"}
    )
    assert resp.json()["data"]["bucket_name"] == "other-bucket"
    assert _key_and_fake["seen"][-1] == ("AKIAEXAMPLE1234", "s3cr3t/value", "ap-south-1")


async def test_bad_credentials_are_not_saved(app, client, _key_and_fake):
    _as(app, "ADMIN")
    _key_and_fake["fail"] = "SignatureDoesNotMatch"
    resp = await client.put("/api/v1/s3/config", params={"project_id": str(PROJECT)}, json=BODY)
    assert resp.json()["code"] == 422 and "secret" in resp.json()["message"].lower()
    assert await S3Config.find_one(S3Config.project_id == PROJECT) is None


async def test_first_save_requires_both_keys(app, client):
    _as(app, "ADMIN")
    resp = await client.put("/api/v1/s3/config", params={"project_id": str(PROJECT)}, json={**BODY, "secret_access_key": ""})
    assert resp.json()["code"] == 422


async def test_without_encryption_key_credentials_are_stored_as_given_and_still_never_returned(app, client, monkeypatch):
    monkeypatch.setenv("S3_CREDENTIALS_ENCRYPTION_KEY", "")
    get_settings.cache_clear()
    _as(app, "ADMIN")
    params = {"project_id": str(PROJECT)}
    saved = await client.put("/api/v1/s3/config", params=params, json=BODY)
    assert saved.json()["code"] == 200 and "s3cr3t" not in saved.text
    stored = await S3Config.find_one(S3Config.project_id == PROJECT)
    assert stored.secret_access_key_enc == "s3cr3t/value"
    assert (await client.get("/api/v1/s3/objects", params=params)).json()["code"] == 200


async def test_supervisor_can_browse_but_not_configure_and_viewer_cannot_browse(app, client):
    _as(app, "ADMIN")
    params = {"project_id": str(PROJECT)}
    await client.put("/api/v1/s3/config", params=params, json=BODY)

    _as(app, "SUPERVISOR")
    assert (await client.get("/api/v1/s3/objects", params=params)).json()["code"] == 200
    assert (await client.put("/api/v1/s3/config", params=params, json=BODY)).json()["code"] == 403
    assert (await client.delete("/api/v1/s3/config", params=params)).json()["code"] == 403

    _as(app, "VIEWER")
    assert (await client.get("/api/v1/s3/objects", params=params)).json()["code"] == 403


async def test_other_projects_are_forbidden_and_unconfigured_is_404(app, client):
    app.dependency_overrides[get_current_identity] = lambda: make_identity("SUPERVISOR", project_ids=[str(PROJECT)])
    other = {"project_id": str(PydanticObjectId())}
    assert (await client.get("/api/v1/s3/objects", params=other)).json()["code"] == 403
    assert (await client.get("/api/v1/s3/objects", params={"project_id": str(PROJECT)})).json()["code"] == 404


async def test_delete_removes_credentials(app, client):
    _as(app, "ADMIN")
    params = {"project_id": str(PROJECT)}
    await client.put("/api/v1/s3/config", params=params, json=BODY)
    resp = await client.delete("/api/v1/s3/config", params=params)
    assert resp.json()["data"]["configured"] is False
    assert await S3Config.find_one(S3Config.project_id == PROJECT) is None
