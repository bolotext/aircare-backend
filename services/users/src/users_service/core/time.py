from datetime import datetime, timezone


def ensure_aware_utc(value: datetime) -> datetime:
    """MongoDB stores BSON datetimes with no timezone; pymongo/motor decode them
    back as naive UTC. Normalize before comparing against timezone-aware values.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
