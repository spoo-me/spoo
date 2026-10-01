"""Unit tests for stats request and response DTOs."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.schemas.dto.requests.stats import (
    ExportQuery,
    LinkExportQuery,
    LinkStatsQuery,
    StatsQuery,
)
from app.schemas.dto.responses.stats import (
    StatsResponse,
    StatsSummary,
    StatsTimeRange,
)

# ── StatsQuery ─────────────────────────────────────────────────────────────────


class TestStatsQuery:
    def test_defaults(self):
        q = StatsQuery.model_validate({})
        assert q.parsed_group_by == ["time"]
        assert q.parsed_metrics == ["clicks", "unique_clicks"]
        assert q.timezone == "UTC"

    @pytest.mark.parametrize("value", ["anon", "all", "invalid"])
    def test_stray_scope_param_silently_ignored(self, value):
        """The scope field is gone; RequestBase is pydantic extra-ignore,
        so an old client sending ANY scope value gets a normal response,
        never a 422."""
        q = StatsQuery.model_validate({"scope": value})
        assert not hasattr(q, "scope")
        assert q.parsed_group_by == ["time"]

    def test_comma_separated_group_by(self):
        q = StatsQuery.model_validate({"group_by": "time,browser,os"})
        assert "time" in q.parsed_group_by
        assert "browser" in q.parsed_group_by

    def test_invalid_group_by_rejected(self):
        with pytest.raises(ValidationError):
            StatsQuery.model_validate({"group_by": "time,language"})

    def test_device_and_utm_dimensions_accepted_in_group_by(self):
        q = StatsQuery.model_validate(
            {"group_by": "device,utm_source,utm_medium,utm_campaign"}
        )
        assert q.parsed_group_by == [
            "device",
            "utm_source",
            "utm_medium",
            "utm_campaign",
        ]

    def test_variant_dimension_accepted_in_group_by_and_filter(self):
        q = StatsQuery.model_validate({"group_by": "variant", "variant": "0,(default)"})
        assert q.parsed_group_by == ["variant"]
        assert q.parsed_filters["variant"] == ["0", "(default)"]

    def test_device_and_utm_filter_params_parsed(self):
        q = StatsQuery.model_validate(
            {
                "device": "mobile,desktop",
                "utm_source": "newsletter",
                "utm_medium": "email,social",
                "utm_campaign": "launch",
            }
        )
        assert q.parsed_filters["device"] == ["mobile", "desktop"]
        assert q.parsed_filters["utm_source"] == ["newsletter"]
        assert q.parsed_filters["utm_medium"] == ["email", "social"]
        assert q.parsed_filters["utm_campaign"] == ["launch"]

    def test_device_and_utm_accepted_in_filters_json(self):
        q = StatsQuery.model_validate(
            {"filters": json.dumps({"device": ["mobile"], "utm_source": ["(none)"]})}
        )
        assert q.parsed_filters["device"] == ["mobile"]
        assert q.parsed_filters["utm_source"] == ["(none)"]

    def test_comma_separated_metrics(self):
        assert StatsQuery.model_validate(
            {"metrics": "unique_clicks"}
        ).parsed_metrics == ["unique_clicks"]

    def test_invalid_metric_rejected(self):
        with pytest.raises(ValidationError):
            StatsQuery.model_validate({"metrics": "clicks,pageviews"})

    def test_filters_json_parsed(self):
        q = StatsQuery.model_validate(
            {"filters": json.dumps({"browser": "Chrome,Firefox"})}
        )
        assert "Chrome" in q.parsed_filters["browser"]

    def test_invalid_filters_json_rejected(self):
        with pytest.raises(ValidationError):
            StatsQuery.model_validate({"filters": "{bad json"})

    def test_individual_filter_params_parsed(self):
        q = StatsQuery.model_validate({"browser": "Chrome", "country": "US,DE"})
        assert q.parsed_filters.get("browser") == ["Chrome"]
        assert "DE" in q.parsed_filters.get("country", [])

    def test_short_code_param_is_a_plain_multi_value_filter(self):
        q = StatsQuery.model_validate({"short_code": "link1,link2"})
        assert q.parsed_filters["short_code"] == ["link1", "link2"]


# ── StatsQuery — url_id filter ─────────────────────────────────────────────────


class TestStatsQueryUrlIdFilter:
    def test_url_id_param_parses_comma_separated(self):
        a, b = "a" * 24, "b" * 24
        q = StatsQuery.model_validate({"url_id": f"{a},{b}"})
        assert q.parsed_filters["url_id"] == [a, b]

    def test_url_id_accepted_in_filters_json(self):
        oid = "c" * 24
        q = StatsQuery.model_validate({"filters": json.dumps({"url_id": [oid]})})
        assert q.parsed_filters["url_id"] == [oid]

    def test_invalid_url_id_param_rejected(self):
        with pytest.raises(ValidationError, match="invalid url_id"):
            StatsQuery.model_validate({"url_id": "not-an-objectid"})

    def test_invalid_url_id_in_filters_json_rejected(self):
        # The JSON path must be validated too, not just the param.
        with pytest.raises(ValidationError, match="invalid url_id"):
            StatsQuery.model_validate({"filters": json.dumps({"url_id": ["nope"]})})

    def test_url_id_filter_survives_stray_anon_scope(self):
        # The anon rejection died with the scope param: every query is
        # owner-stamped now, so the filter is safe under any stray scope=.
        q = StatsQuery.model_validate({"scope": "anon", "url_id": "d" * 24})
        assert q.parsed_filters["url_id"] == ["d" * 24]

    def test_url_id_never_a_group_by_dimension(self):
        with pytest.raises(ValidationError, match="invalid group_by"):
            StatsQuery.model_validate({"group_by": "url_id"})


# ── LinkStatsQuery ─────────────────────────────────────────────────────────────


class TestLinkStatsQuery:
    def test_utm_and_device_group_by_allowed(self):
        # Owner surface — the utm dimensions stay available per-link.
        q = LinkStatsQuery.model_validate(
            {"group_by": "device,utm_source,utm_medium,utm_campaign"}
        )
        assert q.parsed_group_by == [
            "device",
            "utm_source",
            "utm_medium",
            "utm_campaign",
        ]

    def test_short_code_group_by_rejected(self):
        # The path already selects the link — bucketing by it is meaningless.
        with pytest.raises(ValidationError, match="invalid group_by"):
            LinkStatsQuery.model_validate({"group_by": "short_code"})

    def test_has_no_link_identity_params(self):
        # scope/short_code/url_id do not exist on the per-link DTO; stray
        # params are silently ignored (RequestBase extra behaviour).
        q = LinkStatsQuery.model_validate(
            {"scope": "anon", "short_code": "x", "url_id": "a" * 24}
        )
        assert not hasattr(q, "scope")
        assert not hasattr(q, "short_code")
        assert not hasattr(q, "url_id")
        assert q.parsed_filters == {}

    def test_link_identity_json_filters_dropped(self):
        q = LinkStatsQuery.model_validate(
            {
                "filters": json.dumps(
                    {"short_code": ["x"], "url_id": ["a" * 24], "browser": ["Chrome"]}
                )
            }
        )
        assert q.parsed_filters == {"browser": ["Chrome"]}

    def test_dimension_filters_parsed(self):
        q = LinkStatsQuery.model_validate(
            {"browser": "Chrome,Firefox", "utm_source": "(none)"}
        )
        assert q.parsed_filters["browser"] == ["Chrome", "Firefox"]
        assert q.parsed_filters["utm_source"] == ["(none)"]


# ── ExportQuery ────────────────────────────────────────────────────────────────


class TestExportQuery:
    @pytest.mark.parametrize("fmt", ["csv", "xlsx", "json", "xml"])
    def test_valid_format(self, fmt):
        assert ExportQuery.model_validate({"format": fmt}).format == fmt

    def test_missing_format_rejected(self):
        with pytest.raises(ValidationError):
            ExportQuery.model_validate({})

    @pytest.mark.parametrize("fmt", ["pdf", "txt", "docx", ""])
    def test_invalid_format_rejected(self, fmt):
        with pytest.raises(ValidationError):
            ExportQuery.model_validate({"format": fmt})

    def test_inherits_stats_fields(self):
        q = ExportQuery.model_validate({"format": "xlsx", "short_code": "mylink"})
        assert q.parsed_filters["short_code"] == ["mylink"]

    def test_url_id_filter_inherited(self):
        oid = "d" * 24
        q = ExportQuery.model_validate({"format": "json", "url_id": oid})
        assert q.parsed_filters["url_id"] == [oid]


# ── LinkExportQuery ────────────────────────────────────────────────────────────


class TestLinkExportQuery:
    @pytest.mark.parametrize("fmt", ["csv", "xlsx", "json", "xml"])
    def test_valid_format(self, fmt):
        assert LinkExportQuery.model_validate({"format": fmt}).format == fmt

    def test_missing_format_rejected(self):
        with pytest.raises(ValidationError):
            LinkExportQuery.model_validate({})

    def test_invalid_format_rejected(self):
        with pytest.raises(ValidationError):
            LinkExportQuery.model_validate({"format": "pdf"})

    def test_inherits_link_stats_fields(self):
        q = LinkExportQuery.model_validate({"format": "json", "browser": "Chrome"})
        assert q.parsed_filters["browser"] == ["Chrome"]
        assert not hasattr(q, "scope")


# ── StatsResponse ──────────────────────────────────────────────────────────────


class TestStatsResponse:
    def test_serialization(self):
        r = StatsResponse(
            scope="all",
            filters={},
            group_by=["time"],
            timezone="UTC",
            time_range=StatsTimeRange(
                start_date="2024-01-01T00:00:00Z",
                end_date="2024-01-08T00:00:00Z",
            ),
            summary=StatsSummary(
                total_clicks=10,
                unique_clicks=8,
                first_click="2024-01-01T10:00:00Z",
                last_click="2024-01-07T10:00:00Z",
                avg_redirection_time=42.5,
            ),
            metrics={"clicks_by_time": [{"date": "2024-01-01", "clicks": 5}]},
            api_version="v1",
        )
        d = r.model_dump()
        assert d["scope"] == "all"
        assert d["summary"]["total_clicks"] == 10
        assert "clicks_by_time" in d["metrics"]

    def test_summary_avg_redirection_time_is_nullable(self):
        # null = no measurement in the range — distinct from a real 0.0
        s = StatsSummary(total_clicks=0, unique_clicks=0)
        assert s.avg_redirection_time is None
        assert s.model_dump()["avg_redirection_time"] is None
        assert (
            StatsSummary(
                total_clicks=5, unique_clicks=3, avg_redirection_time=12.34
            ).avg_redirection_time
            == 12.34
        )
