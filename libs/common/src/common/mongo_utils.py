"""
Helpers for safely printing/logging Mongo connection config. A MONGO_URI can
carry embedded credentials (mongodb://user:pass@host/...), so it must never
be logged raw — only a credential-redacted summary.
"""
from urllib.parse import urlsplit, urlunsplit


def redact_mongo_uri(uri: str) -> str:
    """`uri` with any userinfo (user:pass@) masked."""
    parsed = urlsplit(uri)
    if "@" not in parsed.netloc:
        return uri
    _, _, host_part = parsed.netloc.rpartition("@")
    return urlunsplit((parsed.scheme, host_part, parsed.path, parsed.query, parsed.fragment))


def mongo_config_summary(uri: str, db_name: str) -> str:
    return f"mongo_uri={redact_mongo_uri(uri)} mongo_db_name={db_name}"
