from functools import lru_cache

from common.env import ENV_FILE
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    # Only the two services that still run as separate processes are proxied;
    # users + reports are part of this app (see app.py).
    mcp_clients_service_url: str | None = None
    voicebot_service_url: str | None = None
    upstream_timeout_seconds: float = 30.0

    # Server bind
    host: str = "0.0.0.0"
    port: int = 8000


@lru_cache
def get_settings() -> Settings:
    return Settings()
