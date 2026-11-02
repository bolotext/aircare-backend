"""Storage encoding for S3 credentials.

By default the access key id and secret are stored as given (plain text in the
`s3_configs` collection). If `S3_CREDENTIALS_ENCRYPTION_KEY` (a Fernet key) is
set, new values are Fernet-encrypted and tagged with an `enc:` prefix; values
without the prefix are read as plain text. So encryption can be switched on
later without migrating anything by hand — re-save the connection to encrypt it.
"""

from cryptography.fernet import Fernet, InvalidToken

from common.errors import ServiceUnavailableError
from reports_service.core.config import get_settings

_PREFIX = "enc:"


def _fernet() -> Fernet | None:
    key = get_settings().s3_credentials_encryption_key
    if not key:
        return None
    try:
        return Fernet(key.encode())
    except ValueError as exc:
        raise ServiceUnavailableError("S3_CREDENTIALS_ENCRYPTION_KEY is not a valid Fernet key.") from exc


def encrypt(value: str) -> str:
    fernet = _fernet()
    return value if fernet is None else _PREFIX + fernet.encrypt(value.encode()).decode()


def decrypt(stored: str) -> str:
    if not stored.startswith(_PREFIX):
        return stored
    fernet = _fernet()
    if fernet is None:
        raise ServiceUnavailableError(
            "Stored S3 credentials are encrypted but S3_CREDENTIALS_ENCRYPTION_KEY isn't set. Save them again."
        )
    try:
        return fernet.decrypt(stored[len(_PREFIX):].encode()).decode()
    except InvalidToken as exc:
        raise ServiceUnavailableError(
            "Stored S3 credentials can't be decrypted (the encryption key changed?). Save them again."
        ) from exc
