from datetime import datetime, timezone


def utcnow() -> datetime:
    """Timezone-aware UTC now. `datetime.utcnow()` is deprecated (Python
    3.12+) and returns a naive datetime that silently compares/serializes
    inconsistently with aware ones — every retrieved_timestamp / detected_at
    default in this codebase goes through this instead."""
    return datetime.now(timezone.utc)
