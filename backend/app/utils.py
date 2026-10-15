"""Small shared helpers."""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Timezone-naive UTC timestamp (consistent across SQLite and Postgres).

    We always *store* naive UTC: SQLite has no real timezone support, and the
    Postgres connection is pinned to UTC so naive values mean UTC there too.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def as_naive_utc(value: datetime | None) -> datetime | None:
    """Normalise any datetime to naive UTC.

    Postgres returns ``TIMESTAMPTZ`` columns as *aware* datetimes while SQLite
    returns naive ones. Converting both to naive UTC lets us do arithmetic on
    values that came from either backend.
    """
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def elapsed_seconds(start: datetime | None, end: datetime | None = None) -> float:
    """Seconds elapsed between ``start`` and ``end`` (default: now, UTC).

    Returns 0.0 when ``start`` is unknown, and never reports negative time.
    """
    start_naive = as_naive_utc(start)
    if start_naive is None:
        return 0.0
    end_naive = as_naive_utc(end) or utcnow()
    return max(0.0, (end_naive - start_naive).total_seconds())
