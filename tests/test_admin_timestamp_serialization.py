"""Admin API timestamp serialization must emit tz-aware UTC.

Regression: timestamps written by SQLite CURRENT_TIMESTAMP (e.g. tokens.created_at,
tokens.last_used_at, request_logs.created_at) are stored as naive UTC with no
offset. JS ``new Date()`` reads a naive date-time as LOCAL time, which skews the
manage-UI clock by the browser's UTC offset (8h on CST). The admin serializer must
attach an explicit UTC offset to every value so clients interpret it correctly.

Naive values are treated as UTC, matching the on-disk convention already used by
``src/services/tokens/at_refresh.should_refresh_at``.
"""
from datetime import datetime, timezone

import pytest

from src.api.admin import to_iso


def test_none_returns_none():
    assert to_iso(None) is None


def test_empty_string_returns_none():
    assert to_iso("") is None
    assert to_iso("   ") is None


def test_naive_datetime_is_tagged_utc():
    # Storage convention: naive == UTC.
    result = to_iso(datetime(2026, 7, 30, 13, 9, 10))
    assert result == "2026-07-30T13:09:10+00:00"


def test_aware_datetime_offset_preserved():
    result = to_iso(datetime(2026, 8, 2, 1, 49, 42, tzinfo=timezone.utc))
    assert result == "2026-08-02T01:49:42+00:00"


def test_current_timestamp_naive_string_gets_offset():
    # Real value observed from a SQLite CURRENT_TIMESTAMP column.
    result = to_iso("2026-07-30 13:09:10.278771")
    assert result == "2026-07-30T13:09:10.278771+00:00"
    assert result.endswith("+00:00")


def test_aware_space_separated_string_is_normalized():
    # Real at_expires value: space separator + offset. Chrome tolerates it, but
    # standardizing to 'T' keeps every browser honest.
    result = to_iso("2026-08-02 01:49:42+00:00")
    assert result == "2026-08-02T01:49:42+00:00"


def test_z_suffix_normalized_to_offset():
    assert to_iso("2026-08-02T01:49:42Z") == "2026-08-02T01:49:42+00:00"


def test_non_timestamp_string_returned_unchanged():
    # Don't crash on opaque strings; pass them through.
    assert to_iso("not-a-date") == "not-a-date"


def test_non_datetime_non_string_passed_through():
    assert to_iso(12345) == 12345


@pytest.mark.parametrize(
    "raw",
    [None, "", "   "],
)
def test_falsy_inputs_yield_none(raw):
    assert to_iso(raw) is None
