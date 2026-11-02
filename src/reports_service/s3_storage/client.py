"""Thin boto3 wrapper. boto3 is synchronous, so every call runs in a worker
thread. Credentials are passed in per call and never logged."""

import asyncio
import logging
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError
from common.errors import BadGatewayError, NotFoundError, UnprocessableEntityError

logger = logging.getLogger(__name__)

_CONFIG = Config(
    signature_version="s3v4",
    connect_timeout=5,
    read_timeout=15,
    retries={"max_attempts": 2, "mode": "standard"},
)


def make_s3_client(access_key_id: str, secret_access_key: str, region_name: str) -> Any:
    """Single seam tests patch; builds a client with the given credentials."""

    return boto3.client(
        "s3",
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name=region_name,
        config=_CONFIG,
    )


_USER_MESSAGES = {
    "InvalidAccessKeyId": "The access key id isn't valid.",
    "SignatureDoesNotMatch": "The secret access key doesn't match the access key id.",
    "AccessDenied": "Access denied: these credentials can't perform this action on the bucket.",
    "NoSuchBucket": "That bucket doesn't exist.",
    "NoSuchKey": "That object doesn't exist.",
    "AuthorizationHeaderMalformed": "The region doesn't match the bucket's region.",
    "PermanentRedirect": "The bucket is in a different region.",
    "301": "The bucket is in a different region.",
    "403": "Access denied: these credentials can't perform this action on the bucket.",
    "404": "That bucket or object doesn't exist.",
}


def _translate(exc: Exception) -> Exception:
    """Maps boto errors to API errors with a sanitized message (never the
    raw exception text, which can echo request details)."""

    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        message = _USER_MESSAGES.get(code)
        if code in ("NoSuchKey", "404") and message:
            return NotFoundError(message)
        if message:
            return UnprocessableEntityError(message)
        logger.warning("S3 error code=%s", code)
        return BadGatewayError("S3 returned an error.")
    if isinstance(exc, (EndpointConnectionError, BotoCoreError)):
        logger.warning("S3 connection error: %s", type(exc).__name__)
        return BadGatewayError("Couldn't reach S3. Check the region name and network access.")
    return exc


async def _run(func, *args, **kwargs):
    try:
        return await asyncio.to_thread(func, *args, **kwargs)
    except (ClientError, BotoCoreError) as exc:
        raise _translate(exc) from None


async def check_bucket(client: Any, bucket: str) -> None:
    await _run(client.head_bucket, Bucket=bucket)


async def list_objects(client: Any, bucket: str, prefix: str, token: str | None, page_size: int) -> dict:
    params: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, "Delimiter": "/", "MaxKeys": page_size}
    if token:
        params["ContinuationToken"] = token
    resp = await _run(client.list_objects_v2, **params)
    return {
        "prefix": prefix,
        "prefixes": [p["Prefix"] for p in resp.get("CommonPrefixes", [])],
        "objects": [
            {
                "key": o["Key"],
                "size": o["Size"],
                "last_modified": o["LastModified"].isoformat() if o.get("LastModified") else None,
                "storage_class": o.get("StorageClass"),
            }
            for o in resp.get("Contents", [])
            # A "folder placeholder" object has the prefix itself as its key.
            if o["Key"] != prefix
        ],
        "next_token": resp.get("NextContinuationToken") if resp.get("IsTruncated") else None,
    }


async def read_object(client: Any, bucket: str, key: str, max_bytes: int) -> dict:
    resp = await _run(client.get_object, Bucket=bucket, Key=key, Range=f"bytes=0-{max_bytes - 1}")
    body = await asyncio.to_thread(resp["Body"].read)
    total = resp.get("ContentRange", "").rsplit("/", 1)[-1]
    size = int(total) if total.isdigit() else len(body)
    return {
        "key": key,
        "content_type": resp.get("ContentType"),
        "size": size,
        "truncated": size > len(body),
        "content": body.decode("utf-8", errors="replace"),
    }


def presign_download(client: Any, bucket: str, key: str, expires_seconds: int) -> str:
    return client.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires_seconds
    )
