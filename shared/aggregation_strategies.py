"""
Aggregation strategies for analytics queries.

Uses the Strategy pattern with a parameterized ``FieldAggregationStrategy``
for simple group-by dimensions, and a dedicated ``TimeAggregationStrategy``
for time-bucketed aggregation that requires genuinely different logic.

Adding a new field dimension (e.g. "language") is a one-line registry entry.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime
from functools import cache
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

import pycountry

from infrastructure.logging import get_logger
from shared.time_bucket_utils import (
    create_mongo_time_bucket_pipeline,
    fill_missing_buckets,
    format_time_bucket_display,
    get_optimal_bucket_config,
)

log = get_logger(__name__)


# ── Helpers ─────────────────────────────────────────────────────────────


@cache
def convert_country_name(country_name: str) -> str:
    """Convert country name to ISO 2-letter country code with caching.

    Returns "XX" if the country cannot be resolved.
    """
    name = country_name.strip()
    try:
        return pycountry.countries.lookup(name).alpha_2
    except (LookupError, ImportError):
        return {"Turkey": "TR", "Russia": "RU"}.get(name, "XX")


# ── Abstract base ───────────────────────────────────────────────────────


class AggregationStrategy(ABC):
    """Abstract base class for aggregation strategies."""

    @abstractmethod
    def build_pipeline(self, base_query: dict[str, Any]) -> list[dict[str, Any]]:
        """Build aggregation pipeline for this strategy."""

    @abstractmethod
    def format_results(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Format the aggregation results."""

    @property
    @abstractmethod
    def dimension_name(self) -> str:
        """Get the dimension name for this strategy."""


# ── Parameterized field strategy (replaces 7 concrete classes) ──────────


class FieldAggregationStrategy(AggregationStrategy):
    """Data-driven strategy for simple group-by dimensions.

    Parameterized by the MongoDB field to group on, the output key name,
    result limit, an optional null default, and an optional per-value
    transform function.

    This single class replaces BrowserAggregationStrategy,
    OSAggregationStrategy, DeviceAggregationStrategy,
    CountryAggregationStrategy, CityAggregationStrategy,
    ReferrerAggregationStrategy, and ShortCodeAggregationStrategy.
    """

    def __init__(
        self,
        mongo_field: str,
        output_key: str,
        limit: int = 20,
        default: str | None = None,
        transform_fn: Callable[[str], str] | None = None,
    ) -> None:
        self._mongo_field = mongo_field
        self._output_key = output_key
        self._limit = limit
        self._default = default
        self._transform_fn = transform_fn

    def build_pipeline(self, base_query: dict[str, Any]) -> list[dict[str, Any]]:
        if self._default is not None:
            group_expr: Any = {"$ifNull": [self._mongo_field, self._default]}
        else:
            group_expr = {"$ifNull": [self._mongo_field, "Unknown"]}

        return [
            {"$match": base_query},
            {
                "$group": {
                    "_id": group_expr,
                    "total_clicks": {"$sum": 1},
                    "unique_clicks": {"$addToSet": "$ip_address"},
                }
            },
            {"$addFields": {"unique_clicks": {"$size": "$unique_clicks"}}},
            {"$sort": {"total_clicks": -1}},
            {"$limit": self._limit},
        ]

    def format_results(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        formatted = []
        for result in results:
            value = result["_id"]
            if self._transform_fn is not None:
                value = self._transform_fn(value)
            formatted.append(
                {
                    self._output_key: value,
                    "total_clicks": result.get("total_clicks", 0),
                    "unique_clicks": result.get("unique_clicks", 0),
                }
            )
        return formatted

    @property
    def dimension_name(self) -> str:
        return self._output_key


# ── Time strategy (genuinely different logic) ───────────────────────────


class TimeAggregationStrategy(AggregationStrategy):
    """Strategy for time-based aggregation with dynamic bucketing."""

    def __init__(
        self,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        time_format: str | None = None,
        timezone: str = "UTC",
    ):
        self.start_date = start_date
        self.end_date = end_date
        self.timezone = timezone

        if time_format:
            self.bucket_config = None
            self.time_format = time_format
        else:
            self.bucket_config = get_optimal_bucket_config(start_date, end_date)
            self.time_format = self.bucket_config.mongo_format

    def build_pipeline(self, base_query: dict[str, Any]) -> list[dict[str, Any]]:
        if self.bucket_config:
            time_bucket_expr = create_mongo_time_bucket_pipeline(
                self.bucket_config, timezone=self.timezone
            )
        else:
            time_bucket_expr = {
                "$dateToString": {
                    "format": self.time_format,
                    "date": "$clicked_at",
                    "timezone": self.timezone,
                }
            }

        return [
            {"$match": base_query},
            {
                "$group": {
                    "_id": time_bucket_expr,
                    "total_clicks": {"$sum": 1},
                    "unique_clicks": {"$addToSet": "$ip_address"},
                }
            },
            {"$addFields": {"unique_clicks": {"$size": "$unique_clicks"}}},
            {"$sort": {"_id": 1}},
        ]

    def format_results(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        formatted_results = []

        for result in results:
            bucket_value = result["_id"]

            if self.bucket_config:
                display_value = format_time_bucket_display(
                    bucket_value, self.bucket_config
                )
            else:
                display_value = bucket_value

            formatted_results.append(
                {
                    "date": display_value,
                    "total_clicks": result.get("total_clicks", 0),
                    "unique_clicks": result.get("unique_clicks", 0),
                    "bucket_strategy": self.bucket_config.strategy.value
                    if self.bucket_config
                    else "legacy",
                    "raw_bucket": bucket_value,
                }
            )

        if self.bucket_config and self.start_date and self.end_date:
            user_tz = ZoneInfo(self.timezone)
            start_in_tz = self.start_date.astimezone(user_tz)
            end_in_tz = self.end_date.astimezone(user_tz)

            formatted_results = fill_missing_buckets(
                formatted_results, start_in_tz, end_in_tz, self.bucket_config
            )

        return formatted_results

    @property
    def dimension_name(self) -> str:
        return "time"

    def get_bucket_info(self) -> dict[str, Any]:
        """Get information about the current bucketing strategy."""
        if self.bucket_config:
            return {
                "strategy": self.bucket_config.strategy.value,
                "interval_minutes": self.bucket_config.interval_minutes,
                "mongo_format": self.bucket_config.mongo_format,
                "display_format": self.bucket_config.display_format,
                "timezone": self.timezone,
            }
        return {
            "strategy": "legacy",
            "mongo_format": self.time_format,
            "display_format": self.time_format,
            "timezone": self.timezone,
        }


# ── Factory ─────────────────────────────────────────────────────────────


class AggregationStrategyFactory:
    """Factory for creating aggregation strategies.

    Field-based strategies are registered as lambda constructors in
    ``_FIELD_STRATEGIES``.  Adding a new dimension is a single entry.
    """

    _FIELD_STRATEGIES: ClassVar[dict[str, Callable[[], AggregationStrategy]]] = {
        "browser": lambda: FieldAggregationStrategy("$browser", "browser", 20),
        "os": lambda: FieldAggregationStrategy("$os", "os", 20),
        # default matches the classifier's own fallback value so clicks
        # recorded before device tracking existed merge into the same
        # "unknown" bucket instead of a separate synthetic "Unknown".
        "device": lambda: FieldAggregationStrategy(
            "$device", "device", 20, default="unknown"
        ),
        "country": lambda: FieldAggregationStrategy(
            "$country", "country", 50, transform_fn=convert_country_name
        ),
        "city": lambda: FieldAggregationStrategy("$city", "city", 50),
        "referrer": lambda: FieldAggregationStrategy(
            "$referrer", "referrer", 30, default="Direct"
        ),
        "short_code": lambda: FieldAggregationStrategy(
            "$meta.short_code", "short_code", 100
        ),
        # "unknown" = clicks recorded before domain stamping existed
        # (time-series buckets can't be backfilled). It can never collide
        # with a real value: domains are always dotted fqdns.
        "domain": lambda: FieldAggregationStrategy(
            "$meta.domain", "domain", 50, default="unknown"
        ),
        # "(none)" = untagged clicks (and clicks recorded before the utm
        # fields existed) — the campaign analogue of referrer's "Direct".
        "utm_source": lambda: FieldAggregationStrategy(
            "$utm_source", "utm_source", 50, default="(none)"
        ),
        "utm_medium": lambda: FieldAggregationStrategy(
            "$utm_medium", "utm_medium", 50, default="(none)"
        ),
        "utm_campaign": lambda: FieldAggregationStrategy(
            "$utm_campaign", "utm_campaign", 50, default="(none)"
        ),
    }

    @classmethod
    def get(cls, strategy_name: str, **kwargs: Any) -> AggregationStrategy:
        """Get an aggregation strategy by name."""
        if strategy_name == "time":
            return TimeAggregationStrategy(**kwargs)

        factory = cls._FIELD_STRATEGIES.get(strategy_name)
        if factory is None:
            raise ValueError(f"Unknown aggregation strategy: {strategy_name}")

        return factory()

    @classmethod
    def get_available_strategies(cls) -> list[str]:
        """Get list of available strategy names."""
        return ["time", *cls._FIELD_STRATEGIES.keys()]
