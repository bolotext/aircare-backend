"""Per-project S3 connection + bucket browser.

`s3.configure` (Admin only) stores the project's credentials encrypted; they
are never returned. `s3.view` (Admin, Supervisor) browses the configured
bucket. Every route re-checks `ensure_project_access`. Credentials and object
contents are never logged.
"""

import logging
from datetime import datetime, timezone

from beanie import PydanticObjectId
from common.errors import NotFoundError, UnprocessableEntityError
from common.openapi import FORBIDDEN_EXAMPLE, UNAUTHORIZED_EXAMPLE, success_and_error_responses
from common.responses import success_response
from common.schemas import SuccessResponse
from fastapi import APIRouter, Body, Depends, Query

from reports_service.core.deps import ensure_project_access, require_permission
from reports_service.core.users_client import IdentityContext
from reports_service.s3_storage import client as s3
from reports_service.s3_storage.crypto import decrypt, encrypt
from reports_service.s3_storage.model import S3Config
from reports_service.s3_storage.schema import (
    S3ConfigIn,
    S3ConfigOut,
    S3DownloadUrl,
    S3Listing,
    S3ObjectContent,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/s3", tags=["s3"])

MAX_PREVIEW_BYTES = 1_000_000
DOWNLOAD_URL_SECONDS = 300
MAX_PAGE_SIZE = 200


def _out(config: S3Config | None) -> S3ConfigOut:
    if config is None:
        return S3ConfigOut(configured=False)
    return S3ConfigOut(
        configured=True,
        access_key_hint=config.access_key_hint,
        region_name=config.region_name,
        bucket_name=config.bucket_name,
        updated_at=config.updated_at,
    )


async def _get_config(project_id: str) -> S3Config | None:
    return await S3Config.find_one(S3Config.project_id == PydanticObjectId(project_id))


async def _connected(project_id: str):
    config = await _get_config(project_id)
    if config is None:
        raise NotFoundError("S3 isn't configured for this project yet.")
    client = s3.make_s3_client(decrypt(config.access_key_id_enc), decrypt(config.secret_access_key_enc), config.region_name)
    return config, client


@router.get(
    "/config",
    response_model=SuccessResponse[S3ConfigOut],
    summary="Get a project's S3 connection (no secrets)",
    description="Returns whether S3 is configured plus region, bucket and the last 4 characters of the access key id. "
    "The secret is never returned. **Errors:** 403 without `s3.view`/project access.",
    responses=success_and_error_responses("Config", None, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE),
)
async def get_config(
    project_id: str = Query(..., description="The project (Mongo ObjectId)."),
    identity: IdentityContext = Depends(require_permission("s3.view")),
) -> dict:
    ensure_project_access(identity, project_id)
    return success_response(data=_out(await _get_config(project_id)))


@router.put(
    "/config",
    response_model=SuccessResponse[S3ConfigOut],
    summary="Save a project's S3 connection",
    description="Admin only. Verifies the credentials against the bucket first (nothing is saved if that fails), "
    "then stores them. Leave `access_key_id` / `secret_access_key` blank on an update to keep the saved "
    "values. **Errors:** 403 without `s3.configure`; 422 if S3 rejects the credentials/region/bucket",
    responses=success_and_error_responses("Saved", None, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE),
)
async def save_config(
    project_id: str = Query(..., description="The project (Mongo ObjectId)."),
    body: S3ConfigIn = Body(...),
    identity: IdentityContext = Depends(require_permission("s3.configure")),
) -> dict:
    ensure_project_access(identity, project_id)
    existing = await _get_config(project_id)
    access_key = body.access_key_id.strip()
    secret = body.secret_access_key.strip()
    if existing is None and not (access_key and secret):
        raise UnprocessableEntityError("Access key id and secret access key are required.")
    if not access_key and existing:
        access_key = decrypt(existing.access_key_id_enc)
    if not secret and existing:
        secret = decrypt(existing.secret_access_key_enc)

    region = body.region_name.strip()
    bucket = body.bucket_name.strip()
    await s3.check_bucket(s3.make_s3_client(access_key, secret, region), bucket)

    values = dict(
        access_key_id_enc=encrypt(access_key),
        secret_access_key_enc=encrypt(secret),
        access_key_hint=access_key[-4:],
        region_name=region,
        bucket_name=bucket,
        updated_by=identity.user_id,
        updated_at=datetime.now(timezone.utc),
    )
    if existing:
        for key, value in values.items():
            setattr(existing, key, value)
        await existing.save()
        config = existing
    else:
        config = S3Config(project_id=PydanticObjectId(project_id), **values)
        await config.insert()
    logger.info("S3 config saved project=%s by=%s", project_id, identity.user_id)
    return success_response(message="S3 connection saved", data=_out(config))


@router.delete(
    "/config",
    response_model=SuccessResponse[S3ConfigOut],
    summary="Remove a project's S3 connection",
    description="Admin only. Deletes the stored credentials. **Errors:** 403 without `s3.configure`.",
    responses=success_and_error_responses("Removed", None, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE),
)
async def delete_config(
    project_id: str = Query(..., description="The project (Mongo ObjectId)."),
    identity: IdentityContext = Depends(require_permission("s3.configure")),
) -> dict:
    ensure_project_access(identity, project_id)
    config = await _get_config(project_id)
    if config is not None:
        await config.delete()
        logger.info("S3 config removed project=%s by=%s", project_id, identity.user_id)
    return success_response(message="S3 connection removed", data=_out(None))


@router.get(
    "/objects",
    response_model=SuccessResponse[S3Listing],
    summary="List objects and folders in the project's bucket",
    description="One level at a time (`/` delimiter): `prefixes` are sub-folders, `objects` the files directly under "
    "`prefix`. Pass `next_token` back as `continuation_token` for the next page. "
    "**Errors:** 403 without `s3.view`; 404 if S3 isn't configured.",
    responses=success_and_error_responses("Listing", None, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE),
)
async def list_objects(
    project_id: str = Query(..., description="The project (Mongo ObjectId)."),
    prefix: str = Query(default="", max_length=1024, description="Folder to list, e.g. `logs/2026/`."),
    continuation_token: str | None = Query(default=None, max_length=2048),
    page_size: int = Query(default=100, ge=1, le=MAX_PAGE_SIZE),
    identity: IdentityContext = Depends(require_permission("s3.view")),
) -> dict:
    ensure_project_access(identity, project_id)
    config, client = await _connected(project_id)
    listing = await s3.list_objects(client, config.bucket_name, prefix, continuation_token, page_size)
    return success_response(data=S3Listing(bucket=config.bucket_name, **listing))


@router.get(
    "/objects/content",
    response_model=SuccessResponse[S3ObjectContent],
    summary="Read an object's content (first 1 MB, as text)",
    description="Returns up to the first 1 MB decoded as UTF-8 (undecodable bytes replaced) and whether it was "
    "truncated. For large or binary files use the download URL. **Errors:** 403 without `s3.view`; 404 if missing.",
    responses=success_and_error_responses("Content", None, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE),
)
async def object_content(
    project_id: str = Query(..., description="The project (Mongo ObjectId)."),
    key: str = Query(..., min_length=1, max_length=1024),
    identity: IdentityContext = Depends(require_permission("s3.view")),
) -> dict:
    ensure_project_access(identity, project_id)
    config, client = await _connected(project_id)
    data = await s3.read_object(client, config.bucket_name, key, MAX_PREVIEW_BYTES)
    return success_response(data=S3ObjectContent(**data))


@router.get(
    "/objects/download-url",
    response_model=SuccessResponse[S3DownloadUrl],
    summary="A short-lived download link for an object",
    description=f"A pre-signed URL valid for {DOWNLOAD_URL_SECONDS} seconds. Anyone holding it can download that one "
    "object until it expires. **Errors:** 403 without `s3.view`; 404 if S3 isn't configured.",
    responses=success_and_error_responses("URL", None, UNAUTHORIZED_EXAMPLE, FORBIDDEN_EXAMPLE),
)
async def download_url(
    project_id: str = Query(..., description="The project (Mongo ObjectId)."),
    key: str = Query(..., min_length=1, max_length=1024),
    identity: IdentityContext = Depends(require_permission("s3.view")),
) -> dict:
    ensure_project_access(identity, project_id)
    config, client = await _connected(project_id)
    url = s3.presign_download(client, config.bucket_name, key, DOWNLOAD_URL_SECONDS)
    logger.info("S3 download url issued project=%s by=%s", project_id, identity.user_id)
    return success_response(data=S3DownloadUrl(url=url, expires_in_seconds=DOWNLOAD_URL_SECONDS))
