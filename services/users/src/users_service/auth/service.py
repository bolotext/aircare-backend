from datetime import datetime, timezone

import jwt
from common.errors import UnauthorizedError
from common.jwt import REFRESH_TOKEN_TYPE

from users_service.auth import repository as refresh_token_repo
from users_service.core.security import (
    create_access_token,
    create_refresh_token,
    decode_own_token,
    hash_refresh_token,
    verify_password,
)
from users_service.core.time import ensure_aware_utc
from users_service.enums import UserStatus
from users_service.users import repository as user_repo
from users_service.users.model import User


async def login(email: str, password: str) -> tuple[User, str, str]:
    user = await user_repo.get_by_email(email)
    if user is None or not verify_password(password, user.hashed_password):
        raise UnauthorizedError("Invalid email or password")
    if user.status != UserStatus.ACTIVE:
        raise UnauthorizedError("This account is not active")

    access_token = create_access_token(str(user.id), str(user.organization_id) if user.organization_id else None, user.role)
    refresh_token = create_refresh_token(str(user.id))
    payload = decode_own_token(refresh_token)
    expires_at = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
    await refresh_token_repo.create_refresh_token(user.id, hash_refresh_token(refresh_token), expires_at)
    await user_repo.record_login(user)

    return user, access_token, refresh_token


async def refresh_access_token(refresh_token: str) -> str:
    try:
        payload = decode_own_token(refresh_token)
    except jwt.ExpiredSignatureError:
        raise UnauthorizedError("Refresh token has expired")
    except jwt.InvalidTokenError:
        raise UnauthorizedError("Invalid refresh token")

    if payload.get("type") != REFRESH_TOKEN_TYPE:
        raise UnauthorizedError("Invalid token type")

    record = await refresh_token_repo.get_valid_by_hash(hash_refresh_token(refresh_token))
    if record is None:
        raise UnauthorizedError("Refresh token has been revoked or does not exist")
    if ensure_aware_utc(record.expires_at) < datetime.now(timezone.utc):
        raise UnauthorizedError("Refresh token has expired")

    user = await user_repo.get_by_id_global(record.user_id)
    if user is None or user.status != UserStatus.ACTIVE:
        raise UnauthorizedError("User is not active")

    return create_access_token(str(user.id), str(user.organization_id) if user.organization_id else None, user.role)


async def logout(refresh_token: str) -> None:
    try:
        decode_own_token(refresh_token)
    except jwt.ExpiredSignatureError:
        raise UnauthorizedError("Refresh token has expired")
    except jwt.InvalidTokenError:
        raise UnauthorizedError("Invalid refresh token")

    record = await refresh_token_repo.get_valid_by_hash(hash_refresh_token(refresh_token))
    if record is None:
        raise UnauthorizedError("Refresh token has been revoked or does not exist")
    await refresh_token_repo.revoke(record)
