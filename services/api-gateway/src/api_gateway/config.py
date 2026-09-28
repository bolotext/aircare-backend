from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "api-gateway"
    log_level: str = "INFO"

    # Upstream service base URLs. Only `users_service_url` is required today.
    users_service_url: str = "http://localhost:45452"
    reports_service_url: str | None = None
    mcp_clients_service_url: str | None = None
    voicebot_service_url: str | None = None

    upstream_timeout_seconds: float = 30.0

    # Comma-separated browser origins allowed to call this gateway directly.
    # Needed because this is the only service a browser talks to (see
    # app.py) and none of the upstream services set CORS headers themselves.
    cors_allowed_origins: str = (
        "http://localhost:5173,http://localhost:5174,http://localhost:5175,http://localhost:4173"
    )

    @property
    def cors_allowed_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
