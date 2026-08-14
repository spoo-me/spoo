"""Unit tests for Phase 9 — StatsService."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from errors import AuthenticationError, ForbiddenError, NotFoundError, ValidationError
from schemas.dto.requests.stats import StatsQuery

# ── Constants ────────────────────────────────────────────────────────────────

OWNER_ID = "aaaaaaaaaaaaaaaaaaaaaaaa"

NOW = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
START = NOW - timedelta(days=7)

NOW_ISO = NOW.isoformat()
START_ISO = START.isoformat()


# ── Helpers ──────────────────────────────────────────────────────────────────


def make_service():
    from services.stats_service import StatsService

    click_repo = AsyncMock()
    url_repo = AsyncMock()
    url_repo.list_claimed_ids.return_value = []
    # Default: aggregate returns empty (no clicks)
    click_repo.aggregate.return_value = []
    return StatsService(click_repo=click_repo, url_repo=url_repo), click_repo, url_repo


def privacy_info(exists=True, private=False, owner_id=OWNER_ID):
    return {"exists": exists, "private": private, "owner_id": owner_id}


def facet_response(
    total=10,
    unique=5,
    first_click=None,
    last_click=None,
    avg_redirect=120.5,
    dimensions=None,
):
    """Build a fake $facet aggregation result."""
    summary = [
        {
            "total_clicks": total,
            "unique_clicks": unique,
            "first_click": first_click or NOW - timedelta(days=1),
            "last_click": last_click or NOW,
            "avg_redirection_time": avg_redirect,
        }
    ]
    result = {"_summary": summary}
    if dimensions:
        result.update(dimensions)
    return [result]  # aggregate() returns a list


def _q(
    scope="anon",
    short_code="abc",
    start_date=None,
    end_date=None,
    group_by="time",
    metrics="clicks",
    timezone_="UTC",
    **filter_kw,
):
    """Build a StatsQuery with sensible defaults for tests."""
    return StatsQuery(
        scope=scope,
        short_code=short_code,
        start_date=start_date if start_date is not None else START_ISO,
        end_date=end_date if end_date is not None else NOW_ISO,
        group_by=group_by,
        metrics=metrics,
        timezone=timezone_,
        **filter_kw,
    )


# ── Tests: date defaults and validation ──────────────────────────────────────


class TestDateHandling:
    @pytest.mark.asyncio
    async def test_default_date_range_applied_when_none(self):
        """When start/end are None, a 7-day window ending now is applied."""
        svc, click_repo, url_repo = make_service()
        url_repo.check_stats_privacy.return_value = privacy_info()
        click_repo.aggregate.return_value = facet_response()

        result = await svc.query(
            query=StatsQuery(
                scope="anon",
                short_code="abc123",
                group_by="time",
                metrics="clicks",
                timezone="UTC",
            ),
            owner_id=OWNER_ID,
        )
        assert "time_range" in result
        assert result["time_range"]["start_date"] is not None
        assert result["time_range"]["end_date"] is not None

    @pytest.mark.asyncio
    async def test_start_date_after_end_date_raises(self):
        svc, _, url_repo = make_service()
        url_repo.check_stats_privacy.return_value = privacy_info()
        future = NOW + timedelta(days=1)

        with pytest.raises(ValidationError, match="start_date must be before end_date"):
            await svc.query(
                query=_q(
                    short_code="abc",
                    start_date=future.isoformat(),
                    end_date=NOW_ISO,
                ),
                owner_id=OWNER_ID,
            )

    @pytest.mark.asyncio
    async def test_date_range_exceeding_90_days_raises(self):
        svc, _, url_repo = make_service()
        url_repo.check_stats_privacy.return_value = privacy_info()

        with pytest.raises(ValidationError, match="date range cannot exceed 90 days"):
            await svc.query(
                query=_q(
                    short_code="abc",
                    start_date=(NOW - timedelta(days=95)).isoformat(),
                    end_date=NOW_ISO,
                ),
                owner_id=OWNER_ID,
            )


# ── Tests: scope / privacy ────────────────────────────────────────────────────


class TestScopeValidation:
    @pytest.mark.asyncio
    async def test_anon_scope_requires_short_code(self):
        svc, _, _ = make_service()

        with pytest.raises(ValidationError, match="short_code is required"):
            await svc.query(
                query=_q(scope="anon", short_code=None),
                owner_id=None,
            )

    @pytest.mark.asyncio
    async def test_anon_scope_short_code_not_found_raises(self):
        svc, _, url_repo = make_service()
        url_repo.check_stats_privacy.return_value = privacy_info(exists=False)

        with pytest.raises(NotFoundError):
            await svc.query(
                query=_q(scope="anon", short_code="ghost"),
                owner_id=None,
            )

    @pytest.mark.asyncio
    async def test_anon_scope_private_stats_unauthenticated_raises(self):
        svc, _, url_repo = make_service()
        url_repo.check_stats_privacy.return_value = privacy_info(
            private=True, owner_id=OWNER_ID
        )

        with pytest.raises(AuthenticationError):
            await svc.query(
                query=_q(scope="anon", short_code="secret"),
                owner_id=None,  # not logged in
            )

    @pytest.mark.asyncio
    async def test_anon_scope_private_stats_wrong_owner_raises(self):
        svc, _, url_repo = make_service()
        url_repo.check_stats_privacy.return_value = privacy_info(
            private=True, owner_id="different_owner_id"
        )

        with pytest.raises(ForbiddenError):
            await svc.query(
                query=_q(scope="anon", short_code="secret"),
                owner_id=OWNER_ID,  # authenticated but not the owner
            )

    @pytest.mark.asyncio
    async def test_anon_scope_private_stats_correct_owner_succeeds(self):
        svc, click_repo, url_repo = make_service()
        url_repo.check_stats_privacy.return_value = privacy_info(
            private=True, owner_id=OWNER_ID
        )
        click_repo.aggregate.return_value = facet_response()

        result = await svc.query(
            query=_q(scope="anon", short_code="secret"),
            owner_id=OWNER_ID,
        )
        assert result["scope"] == "anon"

    @pytest.mark.asyncio
    async def test_all_scope_requires_auth(self):
        svc, _, _ = make_service()

        with pytest.raises(AuthenticationError):
            await svc.query(
                query=_q(scope="all", short_code=None),
                owner_id=None,
            )

    @pytest.mark.asyncio
    async def test_all_scope_with_auth_succeeds(self):
        svc, click_repo, _ = make_service()
        click_repo.aggregate.return_value = facet_response()

        result = await svc.query(
            query=_q(scope="all", short_code=None),
            owner_id=OWNER_ID,
        )
        assert result["scope"] == "all"


# ── Tests: aggregation pipeline structure ────────────────────────────────────


class TestAggregationPipeline:
    @pytest.mark.asyncio
    async def test_single_facet_call_made(self):
        """Only one aggregate() call per query (the $facet pipeline)."""
        svc, click_repo, url_repo = make_service()
        url_repo.check_stats_privacy.return_value = privacy_info()
        click_repo.aggregate.return_value = facet_response()

        await svc.query(
            query=_q(group_by="time,browser"),
            owner_id=OWNER_ID,
        )
        click_repo.aggregate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pipeline_starts_with_match(self):
        svc, click_repo, url_repo = make_service()
        url_repo.check_stats_privacy.return_value = privacy_info()
        click_repo.aggregate.return_value = facet_response()

        await svc.query(
            query=_q(),
            owner_id=OWNER_ID,
        )
        pipeline = click_repo.aggregate.call_args[0][0]
        assert pipeline[0].get("$match") is not None

    @pytest.mark.asyncio
    async def test_pipeline_has_facet_stage(self):
        svc, click_repo, url_repo = make_service()
        url_repo.check_stats_privacy.return_value = privacy_info()
        click_repo.aggregate.return_value = facet_response()

        await svc.query(
            query=_q(group_by="browser,country"),
            owner_id=OWNER_ID,
        )
        pipeline = click_repo.aggregate.call_args[0][0]
        assert pipeline[1].get("$facet") is not None

    @pytest.mark.asyncio
    async def test_facet_contains_summary_and_requested_dimensions(self):
        svc, click_repo, url_repo = make_service()
        url_repo.check_stats_privacy.return_value = privacy_info()
        click_repo.aggregate.return_value = facet_response()

        await svc.query(
            query=_q(group_by="browser,os"),
            owner_id=OWNER_ID,
        )
        facet = click_repo.aggregate.call_args[0][0][1]["$facet"]
        assert "_summary" in facet
        assert "browser" in facet
        assert "os" in facet

    @pytest.mark.asyncio
    async def test_scope_all_adds_owner_id_to_match(self):
        from bson import ObjectId

        svc, click_repo, _ = make_service()
        click_repo.aggregate.return_value = facet_response()

        await svc.query(
            query=_q(scope="all", short_code=None),
            owner_id=OWNER_ID,
        )
        match = click_repo.aggregate.call_args[0][0][0]["$match"]
        assert match["meta.owner_id"] == ObjectId(OWNER_ID)

    @pytest.mark.asyncio
    async def test_scope_anon_adds_short_code_to_match(self):
        svc, click_repo, url_repo = make_service()
        url_repo.check_stats_privacy.return_value = privacy_info()
        click_repo.aggregate.return_value = facet_response()

        await svc.query(
            query=_q(scope="anon", short_code="mycode"),
            owner_id=OWNER_ID,
        )
        match = click_repo.aggregate.call_args[0][0][0]["$match"]
        assert match["meta.short_code"] == "mycode"


# ── Tests: response structure ─────────────────────────────────────────────────


class TestResponseStructure:
    @pytest.mark.asyncio
    async def test_response_has_required_top_level_keys(self):
        svc, click_repo, url_repo = make_service()
        url_repo.check_stats_privacy.return_value = privacy_info()
        click_repo.aggregate.return_value = facet_response()

        result = await svc.query(
            query=_q(),
            owner_id=OWNER_ID,
        )
        for key in (
            "scope",
            "filters",
            "group_by",
            "timezone",
            "metrics",
            "time_range",
            "summary",
            "generated_at",
            "api_version",
        ):
            assert key in result, f"missing key: {key}"

    @pytest.mark.asyncio
    async def test_summary_stats_populated(self):
        svc, click_repo, url_repo = make_service()
        url_repo.check_stats_privacy.return_value = privacy_info()
        click_repo.aggregate.return_value = facet_response(total=50, unique=20)

        result = await svc.query(
            query=_q(),
            owner_id=OWNER_ID,
        )
        assert result["summary"]["total_clicks"] == 50
        assert result["summary"]["unique_clicks"] == 20

    @pytest.mark.asyncio
    async def test_computed_metrics_added_when_clicks_exist(self):
        svc, click_repo, url_repo = make_service()
        url_repo.check_stats_privacy.return_value = privacy_info()
        click_repo.aggregate.return_value = facet_response(total=100, unique=40)

        result = await svc.query(
            query=_q(),
            owner_id=OWNER_ID,
        )
        cm = result.get("computed_metrics", {})
        assert cm["unique_click_rate"] == 40.0
        assert cm["repeat_click_rate"] == 60.0

    @pytest.mark.asyncio
    async def test_anon_scope_includes_short_code_in_response(self):
        svc, click_repo, url_repo = make_service()
        url_repo.check_stats_privacy.return_value = privacy_info()
        click_repo.aggregate.return_value = facet_response()

        result = await svc.query(
            query=_q(scope="anon", short_code="mycode"),
            owner_id=OWNER_ID,
        )
        assert result["short_code"] == "mycode"

    @pytest.mark.asyncio
    async def test_no_results_returns_empty_metrics(self):
        """When aggregate returns nothing, metrics lists are empty."""
        svc, click_repo, url_repo = make_service()
        url_repo.check_stats_privacy.return_value = privacy_info()
        click_repo.aggregate.return_value = []  # no data

        result = await svc.query(
            query=_q(group_by="browser"),
            owner_id=OWNER_ID,
        )
        assert result["metrics"]["clicks_by_browser"] == []
        assert result["summary"]["avg_redirection_time"] is None

    @pytest.mark.asyncio
    async def test_avg_redirection_time_rounded_when_measured(self):
        svc, click_repo, url_repo = make_service()
        url_repo.check_stats_privacy.return_value = privacy_info()
        click_repo.aggregate.return_value = facet_response(avg_redirect=120.456)

        result = await svc.query(query=_q(), owner_id=OWNER_ID)
        assert result["summary"]["avg_redirection_time"] == 120.46

    @pytest.mark.asyncio
    async def test_avg_redirection_time_null_when_no_clicks_in_range(self):
        """Zero clicks in the window (empty _summary facet): null, never 0."""
        svc, click_repo, url_repo = make_service()
        url_repo.check_stats_privacy.return_value = privacy_info()
        click_repo.aggregate.return_value = [{"_summary": [], "time": []}]

        result = await svc.query(query=_q(), owner_id=OWNER_ID)
        assert result["summary"]["total_clicks"] == 0
        assert result["summary"]["avg_redirection_time"] is None

    @pytest.mark.asyncio
    async def test_avg_redirection_time_null_when_clicks_carry_no_measurement(self):
        """Clicks exist but $avg found no redirect_ms values: null, never 0."""
        svc, click_repo, url_repo = make_service()
        url_repo.check_stats_privacy.return_value = privacy_info()
        click_repo.aggregate.return_value = facet_response(total=3, avg_redirect=None)

        result = await svc.query(query=_q(), owner_id=OWNER_ID)
        assert result["summary"]["total_clicks"] == 3
        assert result["summary"]["avg_redirection_time"] is None


# ── Tests: timezone handling ──────────────────────────────────────────────────


class TestTimezone:
    @pytest.mark.asyncio
    async def test_invalid_timezone_falls_back_to_utc(self):
        svc, click_repo, url_repo = make_service()
        url_repo.check_stats_privacy.return_value = privacy_info()
        click_repo.aggregate.return_value = facet_response()

        result = await svc.query(
            query=_q(timezone_="Not/ATimezone"),
            owner_id=OWNER_ID,
        )
        assert result["timezone"] == "UTC"

    @pytest.mark.asyncio
    async def test_timezone_alias_is_normalised(self):
        """Legacy timezone aliases like Asia/Calcutta -> Asia/Kolkata."""
        svc, click_repo, url_repo = make_service()
        url_repo.check_stats_privacy.return_value = privacy_info()
        click_repo.aggregate.return_value = facet_response()

        result = await svc.query(
            query=_q(timezone_="Asia/Calcutta"),
            owner_id=OWNER_ID,
        )
        assert result["timezone"] == "Asia/Kolkata"


# ── Tests: filter query building ─────────────────────────────────────────────


class TestClickQueryBuilding:
    def test_scope_all_produces_owner_id_filter(self):
        from bson import ObjectId

        from services.stats_service import StatsService

        q = StatsService._build_click_query("all", OWNER_ID, None, START, NOW, {})
        assert q["meta.owner_id"] == ObjectId(OWNER_ID)

    def test_scope_anon_produces_short_code_filter(self):
        from services.stats_service import StatsService

        q = StatsService._build_click_query("anon", None, "mycode", START, NOW, {})
        assert q["meta.short_code"] == "mycode"

    def test_time_range_in_query(self):
        from services.stats_service import StatsService

        q = StatsService._build_click_query("all", OWNER_ID, None, START, NOW, {})
        assert q["clicked_at"]["$gte"] == START
        assert q["clicked_at"]["$lte"] == NOW

    def test_dimension_filter_added(self):
        from services.stats_service import StatsService

        q = StatsService._build_click_query(
            "all", OWNER_ID, None, START, NOW, {"browser": ["Chrome", "Firefox"]}
        )
        assert q["browser"] == {"$in": ["Chrome", "Firefox"]}

    def test_referrer_direct_filter_uses_or_clause(self):
        from services.stats_service import StatsService

        q = StatsService._build_click_query(
            "all", OWNER_ID, None, START, NOW, {"referrer": ["Direct"]}
        )
        assert "$or" in q

    def test_short_code_filter_skipped_in_anon_scope(self):
        """short_code filter cannot bypass the scope lock (security)."""
        from services.stats_service import StatsService

        q = StatsService._build_click_query(
            "anon",
            None,
            "locked",
            START,
            NOW,
            {"short_code": ["bypass_attempt"]},
        )
        # meta.short_code must remain the locked value, not the filter value
        assert q["meta.short_code"] == "locked"

    def test_plain_utm_filter_added(self):
        from services.stats_service import StatsService

        q = StatsService._build_click_query(
            "all", OWNER_ID, None, START, NOW, {"utm_source": ["newsletter"]}
        )
        assert q["utm_source"] == {"$in": ["newsletter"]}

    def test_utm_none_sentinel_matches_missing_field(self):
        """ "(none)" must match null/missing utm values, like referrer's
        "Direct"."""
        from services.stats_service import StatsService

        q = StatsService._build_click_query(
            "all", OWNER_ID, None, START, NOW, {"utm_source": ["(none)"]}
        )
        assert q["$or"] == [
            {"utm_source": {"$in": ["(none)"]}},
            {"utm_source": {"$in": [None, ""]}},
            {"utm_source": {"$exists": False}},
        ]

    def test_utm_sentinel_mixed_with_values(self):
        from services.stats_service import StatsService

        q = StatsService._build_click_query(
            "all", OWNER_ID, None, START, NOW, {"utm_medium": ["(none)", "email"]}
        )
        assert q["$or"] == [
            {"utm_medium": {"$in": ["(none)", "email"]}},
            {"utm_medium": {"$in": [None, ""]}},
            {"utm_medium": {"$exists": False}},
        ]

    def test_two_null_sentinel_filters_nest_under_and(self):
        """Two $or groups must combine under $and — a second bare "$or"
        key would silently overwrite the first."""
        from services.stats_service import StatsService

        q = StatsService._build_click_query(
            "all",
            OWNER_ID,
            None,
            START,
            NOW,
            {"referrer": ["Direct"], "utm_source": ["(none)"]},
        )
        assert "$or" not in q
        assert len(q["$and"]) == 2
        assert all("$or" in group for group in q["$and"])

    def test_device_filter_added(self):
        from services.stats_service import StatsService

        q = StatsService._build_click_query(
            "all", OWNER_ID, None, START, NOW, {"device": ["mobile", "tablet"]}
        )
        assert q["device"] == {"$in": ["mobile", "tablet"]}
        assert "$in" not in str(q.get("meta.short_code", ""))

    def test_device_unknown_matches_stored_and_missing(self):
        """ "unknown" is BOTH a stored value (classifier fallback) and the
        sentinel for pre-device-tracking clicks — the filter must match
        both, or it disagrees with what group-by shows."""
        from services.stats_service import StatsService

        q = StatsService._build_click_query(
            "all", OWNER_ID, None, START, NOW, {"device": ["unknown"]}
        )
        assert q["$or"] == [
            {"device": {"$in": ["unknown"]}},
            {"device": {"$in": [None, ""]}},
            {"device": {"$exists": False}},
        ]

    def test_plain_domain_filter_targets_meta_field(self):
        """Domain lives under the time-series metaField — a filter keyed
        on bare "domain" would silently match nothing."""
        from services.stats_service import StatsService

        q = StatsService._build_click_query(
            "all", OWNER_ID, None, START, NOW, {"domain": ["links.example.com"]}
        )
        assert q["meta.domain"] == {"$in": ["links.example.com"]}
        assert "domain" not in q

    def test_domain_unknown_sentinel_matches_missing_field(self):
        """ "unknown" must match clicks recorded before domain stamping
        existed (null/absent meta.domain), on the meta path."""
        from services.stats_service import StatsService

        q = StatsService._build_click_query(
            "all", OWNER_ID, None, START, NOW, {"domain": ["unknown", "spoo.me"]}
        )
        assert q["$or"] == [
            {"meta.domain": {"$in": ["unknown", "spoo.me"]}},
            {"meta.domain": {"$in": [None, ""]}},
            {"meta.domain": {"$exists": False}},
        ]

    def test_domain_groupby_and_filter_agree_on_missing_docs(self):
        """A click doc with no meta.domain lands in the same "unknown"
        bucket for group-by ($ifNull default) and for filtering
        (null-sentinel map)."""
        from services.stats_service import _NULL_SENTINEL_FILTERS
        from shared.aggregation_strategies import AggregationStrategyFactory

        pipeline = AggregationStrategyFactory.get("domain").build_pipeline({})
        group_expr = pipeline[1]["$group"]["_id"]
        assert group_expr["$ifNull"][1] == "unknown"
        assert group_expr["$ifNull"][0]["$cond"][2] == "$meta.domain"
        assert _NULL_SENTINEL_FILTERS["domain"] == "unknown"

    def test_filter_field_paths_agree_with_strategy_registry(self):
        """Drift guard: _FILTER_FIELD_PATHS and the strategy registry are
        two hand-kept copies of each dimension's mongo field. A new
        meta-path dimension that updates the registry but not the map
        reproduces the silent-empty-filter bug the map exists to prevent,
        so pin them against each other. short_code is exempt: its filter
        branch does scope-bypass prevention, not path mapping."""
        from schemas.enums.stats import ALLOWED_FILTERS, StatsDimension
        from services.stats_service import _FILTER_FIELD_PATHS
        from shared.aggregation_strategies import AggregationStrategyFactory

        for dim in ALLOWED_FILTERS:
            if dim == StatsDimension.SHORT_CODE:
                continue
            strategy = AggregationStrategyFactory.get(dim)
            registry_field = strategy._mongo_field.removeprefix("$")
            assert registry_field == _FILTER_FIELD_PATHS.get(dim, dim), dim

    def test_device_groupby_and_filter_agree_on_missing_docs(self):
        """The invariant: a click doc with no device field lands in the
        same "unknown" bucket for group-by (aggregation $ifNull default),
        for filtering (null-sentinel map), and for new writes (classifier
        fallback). If any of the three drifts, widget counts and filter
        counts stop agreeing."""
        from services.click.handlers import classify_device
        from services.stats_service import _NULL_SENTINEL_FILTERS
        from shared.aggregation_strategies import AggregationStrategyFactory

        pipeline = AggregationStrategyFactory.get("device").build_pipeline({})
        group_expr = pipeline[1]["$group"]["_id"]
        assert group_expr["$ifNull"][1] == "unknown"
        assert group_expr["$ifNull"][0]["$cond"][2] == "$device"

        from ua_parser import parse as ua_parse

        classifier_fallback = classify_device(
            ua_parse("SomeExoticClient/1.0"), "SomeExoticClient/1.0"
        )
        assert classifier_fallback == "unknown"
        assert _NULL_SENTINEL_FILTERS["device"] == "unknown"


class TestClaimedLinksArm:
    """scope=ALL carries claimed links' pre-claim history via a url_id arm."""

    @pytest.mark.asyncio
    async def test_empty_claimed_set_keeps_pure_stamp_query(self):
        from bson import ObjectId

        svc, click_repo, url_repo = make_service()
        click_repo.aggregate.return_value = facet_response()
        url_repo.list_claimed_ids.return_value = []

        await svc.query(query=_q(scope="all", short_code=None), owner_id=OWNER_ID)

        match = click_repo.aggregate.call_args[0][0][0]["$match"]
        assert match["meta.owner_id"] == ObjectId(OWNER_ID)
        assert "$or" not in match

    @pytest.mark.asyncio
    async def test_claimed_set_builds_or_arm(self):
        from bson import ObjectId

        svc, click_repo, url_repo = make_service()
        click_repo.aggregate.return_value = facet_response()
        claimed = [ObjectId("f" * 24)]
        url_repo.list_claimed_ids.return_value = claimed

        await svc.query(query=_q(scope="all", short_code=None), owner_id=OWNER_ID)

        url_repo.list_claimed_ids.assert_awaited_once_with(ObjectId(OWNER_ID))
        match = click_repo.aggregate.call_args[0][0][0]["$match"]
        assert "meta.owner_id" not in match
        assert match["$or"] == [
            {"meta.owner_id": ObjectId(OWNER_ID)},
            {"meta.url_id": {"$in": claimed}},
        ]

    @pytest.mark.asyncio
    async def test_claimed_arm_nests_under_and_with_sentinel_filters(self):
        from bson import ObjectId

        from schemas.dto.requests.stats import StatsScope
        from services.stats_service import StatsService

        claimed = [ObjectId("f" * 24)]
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 2, 1, tzinfo=timezone.utc)
        query = StatsService._build_click_query(
            StatsScope.ALL,
            OWNER_ID,
            None,
            start,
            end,
            {"referrer": ["Direct"]},
            claimed_url_ids=claimed,
        )
        # Two $or groups (ownership + null-sentinel referrer) → $and nesting.
        assert "$or" not in query
        assert len(query["$and"]) == 2
        assert query["$and"][0]["$or"][0] == {"meta.owner_id": ObjectId(OWNER_ID)}

    @pytest.mark.asyncio
    async def test_scope_anon_never_consults_claimed_set(self):
        svc, click_repo, url_repo = make_service()
        url_repo.check_stats_privacy.return_value = privacy_info()
        click_repo.aggregate.return_value = facet_response()

        await svc.query(query=_q(scope="anon", short_code="abc1234"), owner_id=None)

        url_repo.list_claimed_ids.assert_not_awaited()
