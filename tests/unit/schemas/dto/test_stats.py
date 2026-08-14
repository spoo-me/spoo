"""Unit tests for stats request and response DTOs."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from schemas.dto.requests.stats import ExportQuery, StatsQuery
from schemas.dto.responses.stats import (
    StatsResponse,
    StatsSummary,
    StatsTimeRange,
)

# ── StatsQuery ─────────────────────────────────────────────────────────────────


class TestStatsQuery:
    def test_defaults(self):
        q = StatsQuery.model_validate({})
        assert q.scope == "all"
        assert q.parsed_group_by == ["time"]
        assert q.parsed_metrics == ["clicks", "unique_clicks"]
        assert q.timezone == "UTC"

    def test_invalid_scope_rejected(self):
        with pytest.raises(ValidationError):
            StatsQuery.model_validate({"scope": "invalid"})

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

    def test_domain_dimension_accepted_in_group_by(self):
        q = StatsQuery.model_validate({"group_by": "domain"})
        assert q.parsed_group_by == ["domain"]

    def test_domain_filter_param_parsed(self):
        q = StatsQuery.model_validate({"domain": "spoo.me,links.example.com"})
        assert q.parsed_filters["domain"] == ["spoo.me", "links.example.com"]

    def test_domain_accepted_in_filters_json(self):
        q = StatsQuery.model_validate({"filters": json.dumps({"domain": ["unknown"]})})
        assert q.parsed_filters["domain"] == ["unknown"]

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
        q = ExportQuery.model_validate({"format": "xlsx", "scope": "all"})
        assert q.scope == "all"


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
