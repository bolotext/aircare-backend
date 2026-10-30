"""JWT verification shared by every service that needs to trust a token issued
by the Users/Auth service (Users itself, the API Gateway, and any future
service such as Agents). Token *issuance* stays in the Users service, since
that's the only service with the password/refresh-token flow — this module
only decodes and validates.
"""

from dataclasses import dataclass
from typing import Any

import jwt

ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_TYPE = "refresh"


@dataclass(frozen=True)
class AccessTokenClaims:
    user_id: str
    organization_id: str | None
    role: str


def decode_token(token: str, secret: str, algorithm: str) -> dict[str, Any]:
    """Raises jwt.InvalidTokenError (or a subclass, e.g. ExpiredSignatureError) on failure."""
    return jwt.decode(token, secret, algorithms=[algorithm])


def decode_access_token(token: str, secret: str, algorithm: str) -> AccessTokenClaims:
    """Decodes and asserts the token is an access token (not a refresh token)."""
    payload = decode_token(token, secret, algorithm)
    if payload.get("type") != ACCESS_TOKEN_TYPE:
        raise jwt.InvalidTokenError("Not an access token")
    return AccessTokenClaims(
        user_id=payload["sub"],
        organization_id=payload.get("organization_id"),
        role=payload["role"],
    )
