"""
Utility functions for determining optimal time bucket intervals based on date ranges.

This module provides dynamic time bucketing strategies for analytics data aggregation,
optimizing granularity based on the time span being analyzed.

This is a relocation of utils/time_bucket_utils.py with no functional changes.
"""

from datetime import datetime, timedelta
from enum import Enum
from typing import Any


class TimeBucketStrategy(Enum):
    """Enumeration of available time bucketing strategies"""

    MINUTE_10 = "10_minute"
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class TimeBucketConfig:
    """Configuration for time bucket aggregation"""

    def __init__(
        self,
        strategy: TimeBucketStrategy,
        mongo_format: str,
        interval_minutes: int,
        display_format: str | None = None,
    ):
        self.strategy = strategy
        self.mongo_format = mongo_format
        self.interval_minutes = interval_minutes
        self.display_format = display_format or mongo_format


# Time bucket configurations for different strategies
BUCKET_CONFIGS = {
    TimeBucketStrategy.MINUTE_10: TimeBucketConfig(
        strategy=TimeBucketStrategy.MINUTE_10,
        mongo_format="%Y-%m-%d %H:%M",
        interval_minutes=10,
        display_format="%Y-%m-%d %H:%M",
    ),
    TimeBucketStrategy.HOURLY: TimeBucketConfig(
        strategy=TimeBucketStrategy.HOURLY,
        mongo_format="%Y-%m-%d %H:00",
        interval_minutes=60,
        display_format="%Y-%m-%d %H:00",
    ),
    TimeBucketStrategy.DAILY: TimeBucketConfig(
        strategy=TimeBucketStrategy.DAILY,
        mongo_format="%Y-%m-%d",
        interval_minutes=1440,  # 24 * 60
        display_format="%Y-%m-%d",
    ),
    TimeBucketStrategy.WEEKLY: TimeBucketConfig(
        strategy=TimeBucketStrategy.WEEKLY,
        mongo_format="%Y-W%U",  # Year-Week format
        interval_minutes=10080,  # 7 * 24 * 60
        display_format="%Y-W%U",
    ),
    TimeBucketStrategy.MONTHLY: TimeBucketConfig(
        strategy=TimeBucketStrategy.MONTHLY,
        mongo_format="%Y-%m",
        interval_minutes=43200,  # Approximate: 30 * 24 * 60
        display_format="%Y-%m",
    ),
}


def determine_optimal_bucket_strategy(
    start_date: datetime, end_date: datetime
) -> TimeBucketStrategy:
    """
    Determine the optimal time bucket strategy based on the date range.

    Strategy Rules:
    - < 1 hour: 10-minute buckets
    - ≤ 24 hours: hourly buckets
    - > 24 hours: daily buckets (for trend analysis up to several months)
    - Future: monthly buckets may be added for yearly retention analytics

    Args:
        start_date: Start of the time range
        end_date: End of the time range

    Returns:
        TimeBucketStrategy: The recommended bucketing strategy
    """
    if not start_date or not end_date:
        return TimeBucketStrategy.DAILY

    time_delta = end_date - start_date
    total_hours = time_delta.total_seconds() / 3600

    # < 1 hour: 10-minute buckets
    if total_hours <= 1:
        return TimeBucketStrategy.MINUTE_10

    # ≤ 24 hours: hourly buckets
    elif total_hours <= 24:
        return TimeBucketStrategy.HOURLY

    # > 24 hours: daily buckets (covers everything from days to months)
    else:
        return TimeBucketStrategy.DAILY


def get_bucket_config(strategy: TimeBucketStrategy) -> TimeBucketConfig:
    """Get the bucket configuration for a given strategy"""
    return BUCKET_CONFIGS[strategy]


def get_optimal_bucket_config(
    start_date: datetime, end_date: datetime
) -> TimeBucketConfig:
    """
    Get the optimal bucket configuration based on date range.

    Args:
        start_date: Start of the time range
        end_date: End of the time range

    Returns:
        TimeBucketConfig: Configuration for the optimal bucketing strategy
    """
    strategy = determine_optimal_bucket_strategy(start_date, end_date)
    return get_bucket_config(strategy)


def create_mongo_time_bucket_pipeline(
    bucket_config: TimeBucketConfig,
    clicked_at_field: str = "clicked_at",
    timezone: str = "UTC",
) -> dict[str, Any]:
    """
    Create MongoDB aggregation pipeline stage for time bucketing.

    For 10-minute buckets, we need special handling to round down to 10-minute intervals.

    Args:
        bucket_config: The bucket configuration to use
        clicked_at_field: Name of the datetime field in the collection
        timezone: IANA timezone for bucketing (default: UTC)

    Returns:
        Dict containing the MongoDB aggregation stage for time bucketing
    """
    if bucket_config.strategy == TimeBucketStrategy.MINUTE_10:
        # For 10-minute buckets, extract parts in target timezone, round minutes, then format
        return {
            "$dateToString": {
                "format": "%Y-%m-%d %H:%M",
                "date": {
                    "$dateFromParts": {
                        "year": {
                            "$year": {
                                "date": f"${clicked_at_field}",
                                "timezone": timezone,
                            }
                        },
                        "month": {
                            "$month": {
                                "date": f"${clicked_at_field}",
                                "timezone": timezone,
                            }
                        },
                        "day": {
                            "$dayOfMonth": {
                                "date": f"${clicked_at_field}",
                                "timezone": timezone,
                            }
                        },
                        "hour": {
                            "$hour": {
                                "date": f"${clicked_at_field}",
                                "timezone": timezone,
                            }
                        },
                        "minute": {
                            "$multiply": [
                                {
                                    "$floor": {
                                        "$divide": [
                                            {
                                                "$minute": {
                                                    "date": f"${clicked_at_field}",
                                                    "timezone": timezone,
                                                }
                                            },
                                            10,
                                        ]
                                    }
                                },
                                10,
                            ]
                        },
                        "timezone": timezone,
                    }
                },
                "timezone": timezone,
            }
        }
    else:
        # For other strategies, use standard dateToString with timezone
        return {
            "$dateToString": {
                "format": bucket_config.mongo_format,
                "date": f"${clicked_at_field}",
                "timezone": timezone,
            }
        }


def format_time_bucket_display(
    bucket_value: str, bucket_config: TimeBucketConfig
) -> str:
    """
    Format time bucket value for display purposes.

    Args:
        bucket_value: The raw bucket value from aggregation
        bucket_config: The bucket configuration used

    Returns:
        Formatted string for display
    """
    try:
        if bucket_config.strategy == TimeBucketStrategy.MINUTE_10:
            # For 10-minute buckets, ensure we show the interval
            dt = datetime.strptime(bucket_value, "%Y-%m-%d %H:%M")
            return dt.strftime("%Y-%m-%d %H:%M")

        elif bucket_config.strategy == TimeBucketStrategy.HOURLY:
            # For hourly buckets, ensure we show the hour
            if ":" not in bucket_value:
                bucket_value += " 00:00"
            dt = datetime.strptime(bucket_value, "%Y-%m-%d %H:%M")
            return dt.strftime("%Y-%m-%d %H:00")

        elif bucket_config.strategy == TimeBucketStrategy.WEEKLY:
            # For weekly buckets, convert to a more readable format
            # MongoDB %U gives week number, we might want to enhance this
            return bucket_value

        else:
            # For daily and monthly, return as-is
            return bucket_value

    except (ValueError, TypeError):
        # If parsing fails, return original value
        return bucket_value


def generate_complete_time_buckets(
    start_date: datetime, end_date: datetime, bucket_config: TimeBucketConfig
) -> list[str]:
    """
    Generate a complete list of time buckets for a given date range.

    This ensures that all time periods are represented in the response,
    even if there are no clicks during those periods.

    Args:
        start_date: Start of the time range
        end_date: End of the time range
        bucket_config: The bucket configuration to use

    Returns:
        List of formatted time bucket strings
    """
    buckets = []
    current = start_date

    if bucket_config.strategy == TimeBucketStrategy.MINUTE_10:
        # Round start time down to nearest 10 minutes
        current = current.replace(second=0, microsecond=0)
        current = current.replace(minute=(current.minute // 10) * 10)

        while current <= end_date:
            bucket_str = current.strftime("%Y-%m-%d %H:%M")
            buckets.append(bucket_str)
            current += timedelta(minutes=10)

    elif bucket_config.strategy == TimeBucketStrategy.HOURLY:
        # Round start time down to nearest hour
        current = current.replace(minute=0, second=0, microsecond=0)

        while current <= end_date:
            bucket_str = current.strftime("%Y-%m-%d %H:00")
            buckets.append(bucket_str)
            current += timedelta(hours=1)

    elif bucket_config.strategy == TimeBucketStrategy.DAILY:
        # Round start time down to start of day
        current = current.replace(hour=0, minute=0, second=0, microsecond=0)

        while current <= end_date:
            bucket_str = current.strftime("%Y-%m-%d")
            buckets.append(bucket_str)
            current += timedelta(days=1)

    elif bucket_config.strategy == TimeBucketStrategy.WEEKLY:
        # Round start time down to start of week (Monday)
        days_since_monday = current.weekday()
        current = current.replace(hour=0, minute=0, second=0, microsecond=0)
        current = current - timedelta(days=days_since_monday)

        while current <= end_date:
            bucket_str = current.strftime("%Y-W%U")
            buckets.append(bucket_str)
            current += timedelta(weeks=1)

    elif bucket_config.strategy == TimeBucketStrategy.MONTHLY:
        # Round start time down to start of month
        current = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        while current <= end_date:
            bucket_str = current.strftime("%Y-%m")
            buckets.append(bucket_str)
            # Move to next month
            if current.month == 12:
                current = current.replace(year=current.year + 1, month=1)
            else:
                current = current.replace(month=current.month + 1)

    return buckets


def fill_missing_buckets(
    actual_results: list[dict[str, Any]],
    start_date: datetime,
    end_date: datetime,
    bucket_config: TimeBucketConfig,
) -> list[dict[str, Any]]:
    """
    Fill in missing time buckets with zero values.

    This ensures continuous time series data even when there are no clicks
    for certain time periods.

    Args:
        actual_results: Results from MongoDB aggregation
        start_date: Start of the time range
        end_date: End of the time range
        bucket_config: The bucket configuration used

    Returns:
        Complete list of results with missing periods filled with zeros
    """
    if not actual_results:
        actual_results = []

    # Generate all expected buckets
    all_buckets = generate_complete_time_buckets(start_date, end_date, bucket_config)

    # Create a lookup map of actual results - use raw_bucket for matching
    actual_map = {}
    for result in actual_results:
        # Use the date field for matching (already formatted)
        bucket_key = result.get("date", "")
        actual_map[bucket_key] = result

    # Fill in complete results
    complete_results = []
    for bucket in all_buckets:
        if bucket in actual_map:
            # Use actual data
            complete_results.append(actual_map[bucket])
        else:
            # Fill with zero values
            zero_result = {
                "date": bucket,  # Use the generated bucket directly
                "total_clicks": 0,
                "unique_clicks": 0,
                "bucket_strategy": bucket_config.strategy.value,
                "raw_bucket": bucket,
            }
            complete_results.append(zero_result)

    return complete_results


def _get_strategy_description(strategy: TimeBucketStrategy) -> str:
    """Get human-readable description for a bucketing strategy"""
    descriptions = {
        TimeBucketStrategy.MINUTE_10: "10-minute intervals for real-time analysis",
        TimeBucketStrategy.HOURLY: "Hourly intervals for daily pattern analysis",
        TimeBucketStrategy.DAILY: "Daily intervals for trend analysis",
        TimeBucketStrategy.WEEKLY: "Weekly intervals for long-term trends",
        TimeBucketStrategy.MONTHLY: "Monthly intervals for yearly comparisons",
    }
    return descriptions.get(strategy, "Unknown strategy")
