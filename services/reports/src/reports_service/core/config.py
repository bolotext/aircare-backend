from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    app_name: str = "reports-service"
    environment: str = "development"
    log_level: str = "INFO"

    # Mongo
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db_name: str = "aircare"

    # Identity is resolved via the Users service's internal introspection
    # endpoint rather than decoding JWTs locally.
    users_service_url: str = "http://localhost:8001"
    internal_api_key: str = "change-me-internal-key"
    introspect_timeout_seconds: float = 5.0

    # Pagination
    default_page_size: int = 20
    max_page_size: int = 100

    # Automailer — internal mail-relay HTTP API. Without it, send_mail raises
    # and the report run is marked "failed".
    alert_api_url: str = ""
    alert_api_timeout_seconds: float = 10.0

    # Daily send time, Asia/Kolkata hour-of-day (0-23).
    report_send_hour_ist: int = 6
    # How often the scheduler loop wakes up to check whether it's time to
    # send yet — not how often it actually sends (see mailer/scheduler.py).
    scheduler_poll_seconds: int = 300
    # Documented simplification: the scheduler has no live way to enumerate
    # "every project" (that lives in the Users service, which has no
    # internal listing endpoint for service-to-service, unauthenticated-user
    # callers), so the set of scopes it emails daily is config-driven rather
    # than discovered. Comma-separated project ids, or the literal "all" for
    # a single combined-projects report. Default: "all" only.
    report_scopes: str = "all"


@lru_cache
def get_settings() -> Settings:
    return Settings()
