from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.shared.datetime_utils import (
    convert_to_gmt,
    parse_datetime,
    to_unix_timestamp,
    to_unix_timestamp_ceil,
)

# ---------------------------------------------------------------------------
# shared.datetime_utils — parse_datetime
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, None),
        (0, datetime(1970, 1, 1, tzinfo=timezone.utc)),
        (1000.5, datetime(1970, 1, 1, 0, 16, 40, tzinfo=timezone.utc)),
        ("2024-01-15T12:00:00Z", datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)),
        (
            "2024-01-15T14:00:00+02:00",
            datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
        ),
        (
            "2024-01-15T12:00:00+05:30",
            datetime(2024, 1, 15, 6, 30, 0, tzinfo=timezone.utc),
        ),
        ("not-a-date", None),
    ],
    ids=[
        "none",
        "epoch_int",
        "epoch_float",
        "iso_z",
        "iso_offset",
        "iso_offset_india",
        "invalid",
    ],
)
def test_parse_datetime(value, expected):
    assert parse_datetime(value) == expected


def test_parse_datetime_naive_assumed_utc():
    dt = parse_datetime("2024-06-01T00:00:00")
    assert dt is not None and dt.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# shared.datetime_utils — convert_to_gmt
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        (
            "2024-01-15T14:00:00+02:00",
            datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
        ),
        (
            "2024-06-01T00:00:00+00:00",
            datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc),
        ),
        ("2024-01-15T14:00:00", None),  # naive → None
    ],
    ids=["offset_converted", "utc_unchanged", "naive_returns_none"],
)
def test_convert_to_gmt(value, expected):
    assert convert_to_gmt(value) == expected


# ---------------------------------------------------------------------------
# shared.datetime_utils — to_unix_timestamp_ceil
# ---------------------------------------------------------------------------


_WHOLE = datetime(2030, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
_WHOLE_TS = int(_WHOLE.timestamp())


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, None),
        (_WHOLE, _WHOLE_TS),
        (_WHOLE.replace(microsecond=900_000), _WHOLE_TS + 1),
        (_WHOLE.replace(microsecond=1), _WHOLE_TS + 1),
        # Naive is UTC, the same rule to_unix_timestamp applies.
        (_WHOLE.replace(microsecond=900_000, tzinfo=None), _WHOLE_TS + 1),
    ],
)
def test_to_unix_timestamp_ceil(value, expected):
    assert to_unix_timestamp_ceil(value) == expected


def test_ceil_agrees_with_floor_on_whole_seconds():
    whole = datetime(2030, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert to_unix_timestamp_ceil(whole) == to_unix_timestamp(whole)
