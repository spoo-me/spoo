from __future__ import annotations

from datetime import datetime

import pytest

from app.shared.time_bucket_utils import (
    BUCKET_CONFIGS,
    TimeBucketStrategy,
    determine_optimal_bucket_strategy,
    fill_missing_buckets,
    format_time_bucket_display,
    generate_complete_time_buckets,
)


@pytest.mark.parametrize(
    "start, end, expected",
    [
        (None, None, TimeBucketStrategy.DAILY),
        (
            datetime(2024, 1, 1, 12, 0),
            datetime(2024, 1, 1, 12, 30),
            TimeBucketStrategy.MINUTE_10,
        ),
        (
            datetime(2024, 1, 1, 12, 0),
            datetime(2024, 1, 1, 13, 0),
            TimeBucketStrategy.MINUTE_10,
        ),  # boundary
        (
            datetime(2024, 1, 1, 0, 0),
            datetime(2024, 1, 1, 12, 0),
            TimeBucketStrategy.HOURLY,
        ),
        (datetime(2024, 1, 1), datetime(2024, 1, 8), TimeBucketStrategy.DAILY),
    ],
    ids=["no_dates", "30min", "exactly_1hr", "12hrs", "7days"],
)
def test_determine_optimal_bucket_strategy(start, end, expected):
    assert determine_optimal_bucket_strategy(start, end) == expected


class TestFillMissingBuckets:
    def test_fills_gap_with_zeros(self):
        config = BUCKET_CONFIGS[TimeBucketStrategy.DAILY]
        actual = [
            {"date": "2024-01-01", "total_clicks": 5, "unique_clicks": 3},
            {"date": "2024-01-03", "total_clicks": 2, "unique_clicks": 2},
        ]
        result = fill_missing_buckets(
            actual, datetime(2024, 1, 1), datetime(2024, 1, 3), config
        )
        dates = [r["date"] for r in result]
        assert "2024-01-02" in dates
        filled = next(r for r in result if r["date"] == "2024-01-02")
        assert filled["total_clicks"] == 0 and filled["unique_clicks"] == 0

    def test_empty_actuals_produces_all_zeros(self):
        config = BUCKET_CONFIGS[TimeBucketStrategy.DAILY]
        result = fill_missing_buckets(
            [], datetime(2024, 1, 1), datetime(2024, 1, 3), config
        )
        assert len(result) == 3
        assert all(r["total_clicks"] == 0 for r in result)


class TestGenerateCompleteTimeBuckets:
    def test_minute_10_rounds_down_and_generates_buckets(self):
        # 12:03 → rounds to 12:00; end at 12:25 → includes 12:00, 12:10, 12:20
        config = BUCKET_CONFIGS[TimeBucketStrategy.MINUTE_10]
        buckets = generate_complete_time_buckets(
            datetime(2024, 1, 1, 12, 3), datetime(2024, 1, 1, 12, 25), config
        )
        assert "2024-01-01 12:00" in buckets
        assert "2024-01-01 12:10" in buckets
        assert "2024-01-01 12:20" in buckets
        assert len(buckets) == 3

    def test_hourly_rounds_down_to_hour(self):
        config = BUCKET_CONFIGS[TimeBucketStrategy.HOURLY]
        buckets = generate_complete_time_buckets(
            datetime(2024, 1, 1, 2, 30), datetime(2024, 1, 1, 4, 0), config
        )
        assert buckets == [
            "2024-01-01 02:00",
            "2024-01-01 03:00",
            "2024-01-01 04:00",
        ]

    def test_daily_generates_one_bucket_per_day(self):
        config = BUCKET_CONFIGS[TimeBucketStrategy.DAILY]
        buckets = generate_complete_time_buckets(
            datetime(2024, 1, 1, 12, 0), datetime(2024, 1, 3, 6, 0), config
        )
        assert buckets == ["2024-01-01", "2024-01-02", "2024-01-03"]

    def test_weekly_rounds_down_to_monday(self):
        # 2024-01-03 is a Wednesday; Monday of that week is 2024-01-01
        config = BUCKET_CONFIGS[TimeBucketStrategy.WEEKLY]
        buckets = generate_complete_time_buckets(
            datetime(2024, 1, 3), datetime(2024, 1, 14), config
        )
        assert len(buckets) == 2
        # Both buckets should be week-formatted strings
        assert all("W" in b for b in buckets)

    def test_monthly_includes_december_and_rolls_to_january(self):
        config = BUCKET_CONFIGS[TimeBucketStrategy.MONTHLY]
        buckets = generate_complete_time_buckets(
            datetime(2023, 11, 15), datetime(2024, 1, 10), config
        )
        assert buckets == ["2023-11", "2023-12", "2024-01"]

    def test_single_bucket_when_start_equals_end(self):
        config = BUCKET_CONFIGS[TimeBucketStrategy.DAILY]
        buckets = generate_complete_time_buckets(
            datetime(2024, 6, 15), datetime(2024, 6, 15), config
        )
        assert buckets == ["2024-06-15"]


class TestFormatTimeBucketDisplay:
    def test_minute_10_returns_formatted_string(self):
        config = BUCKET_CONFIGS[TimeBucketStrategy.MINUTE_10]
        assert (
            format_time_bucket_display("2024-01-01 12:30", config) == "2024-01-01 12:30"
        )

    def test_hourly_returns_hour_format(self):
        config = BUCKET_CONFIGS[TimeBucketStrategy.HOURLY]
        assert (
            format_time_bucket_display("2024-01-01 14:00", config) == "2024-01-01 14:00"
        )

    def test_weekly_returns_as_is(self):
        config = BUCKET_CONFIGS[TimeBucketStrategy.WEEKLY]
        assert format_time_bucket_display("2024-W02", config) == "2024-W02"

    def test_daily_returns_as_is(self):
        config = BUCKET_CONFIGS[TimeBucketStrategy.DAILY]
        assert format_time_bucket_display("2024-01-15", config) == "2024-01-15"

    def test_monthly_returns_as_is(self):
        config = BUCKET_CONFIGS[TimeBucketStrategy.MONTHLY]
        assert format_time_bucket_display("2024-03", config) == "2024-03"

    def test_invalid_input_returns_original_value(self):
        config = BUCKET_CONFIGS[TimeBucketStrategy.MINUTE_10]
        assert format_time_bucket_display("not-a-date", config) == "not-a-date"
