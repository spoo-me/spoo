"""Unit tests for shared.aggregation_strategies."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from shared.aggregation_strategies import (
    AggregationStrategyFactory,
    FieldAggregationStrategy,
    TimeAggregationStrategy,
    convert_country_name,
)

# ---------------------------------------------------------------------------
# convert_country_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, expected",
    [
        ("United States", "US"),
        ("Germany", "DE"),
        ("Japan", "JP"),
        ("Turkey", "TR"),  # manual fallback (pycountry lookup fails)
        ("Russia", "RU"),  # manual fallback
        ("Unknown", "XX"),  # sentinel
        ("NotACountry", "XX"),  # lookup failure → XX
    ],
    ids=["us", "de", "jp", "turkey", "russia", "unknown", "invalid"],
)
def test_convert_country_name(name, expected):
    assert convert_country_name(name) == expected


# ---------------------------------------------------------------------------
# AggregationStrategyFactory
# ---------------------------------------------------------------------------


def test_factory_raises_for_unknown_strategy():
    with pytest.raises(ValueError, match="Unknown aggregation strategy"):
        AggregationStrategyFactory.get("nonexistent")


def test_factory_get_available_strategies():
    assert set(AggregationStrategyFactory.get_available_strategies()) == {
        "time",
        "browser",
        "os",
        "device",
        "country",
        "city",
        "referrer",
        "short_code",
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "domain",
    }


def test_factory_passes_kwargs_to_time_strategy():
    """Factory should forward **kwargs to strategy constructor."""
    s = AggregationStrategyFactory.get("time", time_format="%Y-%m-%d")
    assert isinstance(s, TimeAggregationStrategy)
    assert s.time_format == "%Y-%m-%d"
    assert s.bucket_config is None  # legacy mode when time_format given


def test_factory_returns_field_strategy_for_non_time():
    """Factory should return FieldAggregationStrategy for non-time dimensions."""
    for name in (
        "browser",
        "os",
        "device",
        "country",
        "city",
        "referrer",
        "short_code",
    ):
        s = AggregationStrategyFactory.get(name)
        assert isinstance(s, FieldAggregationStrategy), (
            f"{name} should be FieldAggregationStrategy"
        )


# ---------------------------------------------------------------------------
# Helpers to create strategies via the factory (replaces old class constructors)
# ---------------------------------------------------------------------------


def _strategy(name: str) -> FieldAggregationStrategy:
    return AggregationStrategyFactory.get(name)


# ---------------------------------------------------------------------------
# build_pipeline — correct MongoDB field grouping
# ---------------------------------------------------------------------------


_BASE_QUERY = {"meta.owner_id": "user123"}


@pytest.mark.parametrize(
    "strategy_name, expected_field",
    [
        ("browser", "$browser"),
        ("os", "$os"),
        ("device", "$device"),
        ("country", "$country"),
        ("city", "$city"),
        ("referrer", "$referrer"),
    ],
)
def test_pipeline_groups_by_correct_field(strategy_name, expected_field):
    """Each strategy must group by its own document field."""
    strategy = _strategy(strategy_name)
    pipeline = strategy.build_pipeline(_BASE_QUERY)
    group_stage = next(s["$group"] for s in pipeline if "$group" in s)
    group_id = group_stage["_id"]
    # _id is {"$ifNull": [<field>, <default>]}
    assert group_id["$ifNull"][0] == expected_field


def test_short_code_pipeline_groups_by_nested_field():
    """ShortCode must group by nested meta.short_code, not a top-level field."""
    pipeline = _strategy("short_code").build_pipeline(_BASE_QUERY)
    group_stage = next(s["$group"] for s in pipeline if "$group" in s)
    assert group_stage["_id"]["$ifNull"][0] == "$meta.short_code"


def test_domain_pipeline_groups_by_nested_field():
    """Domain must group by nested meta.domain, not a top-level field."""
    pipeline = _strategy("domain").build_pipeline(_BASE_QUERY)
    group_stage = next(s["$group"] for s in pipeline if "$group" in s)
    assert group_stage["_id"]["$ifNull"][0] == "$meta.domain"


def test_referrer_pipeline_uses_direct_as_null_fallback():
    """Referrer should fall back to 'Direct' (not 'Unknown') for null values."""
    pipeline = _strategy("referrer").build_pipeline(_BASE_QUERY)
    group_stage = next(s["$group"] for s in pipeline if "$group" in s)
    assert group_stage["_id"]["$ifNull"][1] == "Direct"


@pytest.mark.parametrize(
    "strategy_name, expected_null_fallback",
    [
        ("browser", "Unknown"),
        ("os", "Unknown"),
        # lowercase: must equal the classifier's own fallback so missing
        # docs and classified-unknown docs share one bucket
        ("device", "unknown"),
        ("country", "Unknown"),
        ("city", "Unknown"),
        ("utm_source", "(none)"),
        ("utm_medium", "(none)"),
        ("utm_campaign", "(none)"),
        # clicks recorded before domain stamping existed
        ("domain", "unknown"),
    ],
)
def test_pipeline_uses_unknown_as_null_fallback(strategy_name, expected_null_fallback):
    pipeline = _strategy(strategy_name).build_pipeline(_BASE_QUERY)
    group_stage = next(s["$group"] for s in pipeline if "$group" in s)
    assert group_stage["_id"]["$ifNull"][1] == expected_null_fallback


# ---------------------------------------------------------------------------
# build_pipeline — limit values differ by strategy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "strategy_name, expected_limit",
    [
        ("browser", 20),
        ("os", 20),
        ("device", 20),
        ("country", 50),
        ("city", 50),
        ("referrer", 30),
        ("short_code", 100),
        ("domain", 50),
    ],
)
def test_pipeline_limit_values(strategy_name, expected_limit):
    """Each strategy has a documented result cap — verify the actual $limit value."""
    pipeline = _strategy(strategy_name).build_pipeline(_BASE_QUERY)
    limit_stage = next(s["$limit"] for s in pipeline if "$limit" in s)
    assert limit_stage == expected_limit


# ---------------------------------------------------------------------------
# build_pipeline — unique_clicks uses $addToSet + $size (dedup by IP)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "strategy_name",
    ["browser", "os", "device", "country", "city", "referrer", "short_code"],
)
def test_pipeline_deduplicates_unique_clicks_by_ip(strategy_name):
    """unique_clicks must be computed via $addToSet on ip_address, then $size."""
    strategy = _strategy(strategy_name)
    pipeline = strategy.build_pipeline(_BASE_QUERY)
    group_stage = next(s["$group"] for s in pipeline if "$group" in s)
    add_fields = next(s["$addFields"] for s in pipeline if "$addFields" in s)

    assert group_stage["unique_clicks"] == {"$addToSet": "$ip_address"}
    assert add_fields["unique_clicks"] == {"$size": "$unique_clicks"}


def test_time_pipeline_deduplicates_unique_clicks_by_ip():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    end = datetime(2025, 1, 7, tzinfo=timezone.utc)
    pipeline = TimeAggregationStrategy(start_date=start, end_date=end).build_pipeline(
        _BASE_QUERY
    )
    group_stage = next(s["$group"] for s in pipeline if "$group" in s)
    add_fields = next(s["$addFields"] for s in pipeline if "$addFields" in s)

    assert group_stage["unique_clicks"] == {"$addToSet": "$ip_address"}
    assert add_fields["unique_clicks"] == {"$size": "$unique_clicks"}


# ---------------------------------------------------------------------------
# TimeAggregationStrategy — pipeline modes
# ---------------------------------------------------------------------------


def test_time_pipeline_legacy_uses_date_to_string():
    strategy = TimeAggregationStrategy(time_format="%Y-%m-%d")
    pipeline = strategy.build_pipeline(_BASE_QUERY)
    group_stage = next(s["$group"] for s in pipeline if "$group" in s)
    assert "$dateToString" in group_stage["_id"]
    assert group_stage["_id"]["$dateToString"]["format"] == "%Y-%m-%d"


def test_time_pipeline_dynamic_format_coarsens_for_long_range():
    """A year-long range should produce a coarser (monthly) format than a week-long range."""
    week = TimeAggregationStrategy(
        start_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
        end_date=datetime(2025, 1, 7, tzinfo=timezone.utc),
    )
    year = TimeAggregationStrategy(
        start_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
        end_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )

    def get_format(strategy):
        pipeline = strategy.build_pipeline(_BASE_QUERY)
        group_stage = next(s["$group"] for s in pipeline if "$group" in s)
        return group_stage["_id"]["$dateToString"]["format"]

    week_fmt = get_format(week)
    year_fmt = get_format(year)
    # Year-range format should be shorter/coarser than week-range format
    assert len(year_fmt) <= len(week_fmt)


def test_time_pipeline_respects_timezone():
    strategy = TimeAggregationStrategy(
        time_format="%Y-%m-%d", timezone="America/New_York"
    )
    pipeline = strategy.build_pipeline(_BASE_QUERY)
    group_stage = next(s["$group"] for s in pipeline if "$group" in s)
    assert group_stage["_id"]["$dateToString"]["timezone"] == "America/New_York"


# ---------------------------------------------------------------------------
# TimeAggregationStrategy — get_bucket_info
# ---------------------------------------------------------------------------


def test_get_bucket_info_dynamic_has_strategy_and_timezone():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    end = datetime(2025, 1, 7, tzinfo=timezone.utc)
    info = TimeAggregationStrategy(start_date=start, end_date=end).get_bucket_info()
    assert info["strategy"] != "legacy"
    assert info["timezone"] == "UTC"
    assert "interval_minutes" in info


def test_get_bucket_info_legacy_returns_format():
    info = TimeAggregationStrategy(time_format="%Y-%m-%d").get_bucket_info()
    assert info["strategy"] == "legacy"
    assert info["mongo_format"] == "%Y-%m-%d"


# ---------------------------------------------------------------------------
# format_results — output shape
# ---------------------------------------------------------------------------


_RAW = [{"_id": "Chrome", "total_clicks": 10, "unique_clicks": 7}]


@pytest.mark.parametrize(
    "strategy_name, key",
    [
        ("browser", "browser"),
        ("os", "os"),
        ("device", "device"),
        ("city", "city"),
        ("referrer", "referrer"),
        ("short_code", "short_code"),
        ("domain", "domain"),
    ],
)
def test_format_results_renames_id_to_dimension_key(strategy_name, key):
    raw = [{"_id": "value", "total_clicks": 5, "unique_clicks": 3}]
    result = _strategy(strategy_name).format_results(raw)[0]
    assert key in result
    assert result[key] == "value"
    assert "_id" not in result


def test_country_format_results_converts_name_to_code():
    result = _strategy("country").format_results(
        [{"_id": "Germany", "total_clicks": 4, "unique_clicks": 2}]
    )[0]
    assert result["country"] == "DE"
    assert "_id" not in result


def test_country_format_results_unknown_maps_to_xx():
    result = _strategy("country").format_results(
        [{"_id": "Unknown", "total_clicks": 1, "unique_clicks": 1}]
    )[0]
    assert result["country"] == "XX"


def test_format_results_missing_counts_default_to_zero():
    result = _strategy("browser").format_results([{"_id": "Firefox"}])[0]
    assert result["total_clicks"] == 0
    assert result["unique_clicks"] == 0


def test_format_results_empty_list():
    for name in (
        "browser",
        "os",
        "device",
        "country",
        "city",
        "referrer",
        "short_code",
    ):
        assert _strategy(name).format_results([]) == []


# ---------------------------------------------------------------------------
# TimeAggregationStrategy — format_results
# ---------------------------------------------------------------------------


def test_time_format_results_legacy_returns_date_and_bucket_strategy():
    s = TimeAggregationStrategy(time_format="%Y-%m-%d")
    results = s.format_results(
        [{"_id": "2025-01-03", "total_clicks": 5, "unique_clicks": 3}]
    )
    assert results[0]["date"] == "2025-01-03"
    assert results[0]["bucket_strategy"] == "legacy"
    assert results[0]["total_clicks"] == 5


def test_time_format_results_dynamic_adds_bucket_strategy():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    end = datetime(2025, 1, 7, tzinfo=timezone.utc)
    s = TimeAggregationStrategy(start_date=start, end_date=end)
    raw = [{"_id": "2025-01-03", "total_clicks": 5, "unique_clicks": 3}]
    results = s.format_results(raw)
    assert len(results) > 0  # fill_missing_buckets may add gap entries
    assert results[0]["bucket_strategy"] != "legacy"


def test_time_format_results_empty():
    assert TimeAggregationStrategy().format_results([]) == []
