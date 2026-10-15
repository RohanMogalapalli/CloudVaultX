"""Tests for the datetime helpers.

Postgres hands back ``TIMESTAMPTZ`` values as timezone-*aware* datetimes while
SQLite (and our own ``utcnow()``) produce naive ones. Subtracting the two
raises ``TypeError``, which used to crash node recovery and silently kill the
monitoring loop - these tests pin that fix down.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.utils import as_naive_utc, elapsed_seconds, utcnow


def test_utcnow_is_naive_utc():
    value = utcnow()
    assert value.tzinfo is None
    assert abs((datetime.now(timezone.utc).replace(tzinfo=None) - value).total_seconds()) < 5


def test_as_naive_utc_converts_aware_values():
    aware = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert as_naive_utc(aware) == datetime(2026, 1, 2, 3, 4, 5)
    assert as_naive_utc(aware).tzinfo is None


def test_as_naive_utc_converts_other_offsets():
    offset = timezone(timedelta(hours=5, minutes=30))
    aware = datetime(2026, 1, 2, 8, 30, 0, tzinfo=offset)  # 03:00 UTC
    assert as_naive_utc(aware) == datetime(2026, 1, 2, 3, 0, 0)


def test_as_naive_utc_passes_through_naive_and_none():
    naive = datetime(2026, 1, 1, 0, 0, 0)
    assert as_naive_utc(naive) == naive
    assert as_naive_utc(None) is None


def test_elapsed_seconds_with_aware_start_naive_end():
    """The exact Postgres scenario that used to crash."""
    start = datetime.now(timezone.utc) - timedelta(seconds=30)
    result = elapsed_seconds(start)
    assert 29 <= result <= 32


def test_elapsed_seconds_with_naive_start_aware_end():
    start = utcnow() - timedelta(seconds=10)
    end = datetime.now(timezone.utc)
    result = elapsed_seconds(start, end)
    assert 9 <= result <= 12


def test_elapsed_seconds_with_mixed_aware_and_naive_both_sides():
    start = datetime.now(timezone.utc) - timedelta(minutes=2)
    end = utcnow()
    assert 119 <= elapsed_seconds(start, end) <= 121


def test_elapsed_seconds_returns_zero_for_unknown_start():
    assert elapsed_seconds(None) == 0.0


def test_elapsed_seconds_never_negative():
    future = utcnow() + timedelta(minutes=5)
    assert elapsed_seconds(future) == 0.0
