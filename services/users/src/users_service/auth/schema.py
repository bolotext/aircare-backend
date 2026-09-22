from pydantic import BaseModel, ConfigDict, EmailStr, Field


class LoginRequest(BaseModel):
    """Credentials for `POST /auth/login`."""

    model_config = ConfigDict(json_schema_extra={"example": {"email": "admin@example.com", "password": "change-me-strong-password"}})

    email: EmailStr = Field(description="The user's account email address.")
    password: str = Field(description="The user's password, in plaintext over TLS (never logged or echoed back).")


class RefreshRequest(BaseModel):
    """Exchanges a still-valid refresh token for a new access token."""

    refresh_token: str = Field(description="A refresh token previously issued by `POST /auth/login`.")


class LogoutRequest(BaseModel):
    """Revokes a refresh token so it can no longer be used to mint new access tokens."""

    refresh_token: str = Field(description="The refresh token to revoke.")


class TokenResponse(BaseModel):
    """Issued on successful login — the access token authorizes subsequent requests (see the **Authorize**
    button above); the refresh token is used with `POST /auth/refresh` to get a new one once it expires."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
                "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
                "token_type": "bearer",
            }
        }
    )

    access_token: str = Field(description="Short-lived JWT — pass as `Authorization: Bearer <access_token>` on every subsequent authenticated request.")
    refresh_token: str = Field(description="Longer-lived JWT — exchange it via `POST /auth/refresh` for a new access token once the current one expires.")
    token_type: str = Field(default="bearer", description="Always `\"bearer\"`.")


class AccessTokenResponse(BaseModel):
    """A freshly minted access token, issued by `POST /auth/refresh`."""

    access_token: str = Field(description="New short-lived JWT — pass as `Authorization: Bearer <access_token>` on subsequent requests.")
    token_type: str = Field(default="bearer", description="Always `\"bearer\"`.")
