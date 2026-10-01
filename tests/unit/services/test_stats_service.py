"""Unit tests for Phase 9 — StatsService."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.errors import AuthenticationError, ValidationError
from app.schemas.dto.requests.stats import StatsQuery

# ── Constants ────────────────────────────────────────────────────────────────

OWNER_ID = "aaaaaaaaaaaaaaaaaaaaaaaa"

NOW = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
START = NOW - timedelta(days=7)

NOW_ISO = NOW.isoformat()
START_ISO = START.isoformat()


# ── Helpers ──────────────────────────────────────────────────────────────────


def make_service():
    from app.services.stats_service import StatsService

    click_repo = AsyncMock()
    url_repo = AsyncMock()
    url_repo.list_claimed_ids.return_value = []
    # Default: aggregate returns empty (no clicks)
    click_repo.aggregate.return_value = []
    return StatsService(click_repo=click_repo, url_repo=url_repo), click_repo, url_repo


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
    short_code=None,
    start_date=None,
    end_date=None,
    group_by="time",
    metrics="clicks",
    timezone_="UTC",
    **filter_kw,
):
    """Build a StatsQuery with sensible defaults for tests."""
    return StatsQuery(
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
        svc, click_repo, _ = make_service()
        click_repo.aggregate.return_value = facet_response()

        result = await svc.query(
            query=StatsQuery(
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
        svc, _, _ = make_service()
        future = NOW + timedelta(days=1)

        with pytest.raises(ValidationError, match="start_date must be before end_date"):
            await svc.query(
                query=_q(
                    start_date=future.isoformat(),
                    end_date=NOW_ISO,
                ),
                owner_id=OWNER_ID,
            )

    @pytest.mark.asyncio
    async def test_date_range_exceeding_90_days_raises(self):
        svc, _, _ = make_service()

        with pytest.raises(ValidationError, match="date range cannot exceed 90 days"):
            await svc.query(
                query=_q(
                    start_date=(NOW - timedelta(days=95)).isoformat(),
                    end_date=NOW_ISO,
                ),
                owner_id=OWNER_ID,
            )


# ── Tests: authentication ─────────────────────────────────────────────────────


class TestAuthValidation:
    @pytest.mark.asyncio
    async def test_unauthenticated_raises(self):
        """Defence in depth — the route already requires auth."""
        svc, _, _ = make_service()

        with pytest.raises(AuthenticationError):
            await svc.query(query=_q(), owner_id=None)

    @pytest.mark.asyncio
    async def test_authenticated_succeeds(self):
        svc, click_repo, _ = make_service()
        click_repo.aggregate.return_value = facet_response()

        result = await svc.query(query=_q(), owner_id=OWNER_ID)
        assert result["scope"] == "all"


# ── Tests: aggregation pipeline structure ────────────────────────────────────


class TestAggregationPipeline:
    @pytest.mark.asyncio
    async def test_single_facet_call_made(self):
        """Only one aggregate() call per query (the $facet pipeline)."""
        svc, click_repo, _ = make_service()
        click_repo.aggregate.return_value = facet_response()

        await svc.query(
            query=_q(group_by="time,browser"),
            owner_id=OWNER_ID,
        )
        click_repo.aggregate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pipeline_starts_with_match(self):
        svc, click_repo, _ = make_service()
        click_repo.aggregate.return_value = facet_response()

        await svc.query(
            query=_q(),
            owner_id=OWNER_ID,
        )
        pipeline = click_repo.aggregate.call_args[0][0]
        assert pipeline[0].get("$match") is not None

    @pytest.mark.asyncio
    async def test_pipeline_has_facet_stage(self):
        svc, click_repo, _ = make_service()
        click_repo.aggregate.return_value = facet_response()

        await svc.query(
            query=_q(group_by="browser,country"),
            owner_id=OWNER_ID,
        )
        pipeline = click_repo.aggregate.call_args[0][0]
        assert pipeline[1].get("$facet") is not None

    @pytest.mark.asyncio
    async def test_facet_contains_summary_and_requested_dimensions(self):
        svc, click_repo, _ = make_service()
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
    async def test_match_scopes_by_owner_id(self):
        from bson import ObjectId

        svc, click_repo, _ = make_service()
        click_repo.aggregate.return_value = facet_response()

        await svc.query(
            query=_q(),
            owner_id=OWNER_ID,
        )
        match = click_repo.aggregate.call_args[0][0][0]["$match"]
        assert match["meta.owner_id"] == ObjectId(OWNER_ID)

    @pytest.mark.asyncio
    async def test_short_code_param_slices_the_owner_aggregate(self):
        """short_code is a plain filter now — always inside the owner stamp."""
        from bson import ObjectId

        svc, click_repo, _ = make_service()
        click_repo.aggregate.return_value = facet_response()

        await svc.query(
            query=_q(short_code="mycode"),
            owner_id=OWNER_ID,
        )
        match = click_repo.aggregate.call_args[0][0][0]["$match"]
        assert match["meta.owner_id"] == ObjectId(OWNER_ID)
        assert match["meta.short_code"] == {"$in": ["mycode"]}


# ── Tests: response structure ─────────────────────────────────────────────────


class TestResponseStructure:
    @pytest.mark.asyncio
    async def test_response_has_required_top_level_keys(self):
        svc, click_repo, _ = make_service()
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
    async def test_response_scope_pinned_to_all(self):
        """The wire keeps its scope key ("all") — the FE adapter reads it."""
        svc, click_repo, _ = make_service()
        click_repo.aggregate.return_value = facet_response()

        result = await svc.query(query=_q(), owner_id=OWNER_ID)
        assert result["scope"] == "all"
        assert "short_code" not in result

    @pytest.mark.asyncio
    async def test_summary_stats_populated(self):
        svc, click_repo, _ = make_service()
        click_repo.aggregate.return_value = facet_response(total=50, unique=20)

        result = await svc.query(
            query=_q(),
            owner_id=OWNER_ID,
        )
        assert result["summary"]["total_clicks"] == 50
        assert result["summary"]["unique_clicks"] == 20

    @pytest.mark.asyncio
    async def test_computed_metrics_added_when_clicks_exist(self):
        svc, click_repo, _ = make_service()
        click_repo.aggregate.return_value = facet_response(total=100, unique=40)

        result = await svc.query(
            query=_q(),
            owner_id=OWNER_ID,
        )
        cm = result.get("computed_metrics", {})
        assert cm["unique_click_rate"] == 40.0
        assert cm["repeat_click_rate"] == 60.0

    @pytest.mark.asyncio
    async def test_no_results_returns_empty_metrics(self):
        """When aggregate returns nothing, metrics lists are empty."""
        svc, click_repo, _ = make_service()
        click_repo.aggregate.return_value = []  # no data

        result = await svc.query(
            query=_q(group_by="browser"),
            owner_id=OWNER_ID,
        )
        assert result["metrics"]["clicks_by_browser"] == []
        assert result["summary"]["avg_redirection_time"] is None

    @pytest.mark.asyncio
    async def test_avg_redirection_time_rounded_when_measured(self):
        svc, click_repo, _ = make_service()
        click_repo.aggregate.return_value = facet_response(avg_redirect=120.456)

        result = await svc.query(query=_q(), owner_id=OWNER_ID)
        assert result["summary"]["avg_redirection_time"] == 120.46

    @pytest.mark.asyncio
    async def test_avg_redirection_time_null_when_no_clicks_in_range(self):
        """Zero clicks in the window (empty _summary facet): null, never 0."""
        svc, click_repo, _ = make_service()
        click_repo.aggregate.return_value = [{"_summary": [], "time": []}]

        result = await svc.query(query=_q(), owner_id=OWNER_ID)
        assert result["summary"]["total_clicks"] == 0
        assert result["summary"]["avg_redirection_time"] is None

    @pytest.mark.asyncio
    async def test_avg_redirection_time_null_when_clicks_carry_no_measurement(self):
        """Clicks exist but $avg found no redirect_ms values: null, never 0."""
        svc, click_repo, _ = make_service()
        click_repo.aggregate.return_value = facet_response(total=3, avg_redirect=None)

        result = await svc.query(query=_q(), owner_id=OWNER_ID)
        assert result["summary"]["total_clicks"] == 3
        assert result["summary"]["avg_redirection_time"] is None


# ── Tests: timezone handling ──────────────────────────────────────────────────


class TestTimezone:
    @pytest.mark.asyncio
    async def test_invalid_timezone_falls_back_to_utc(self):
        svc, click_repo, _ = make_service()
        click_repo.aggregate.return_value = facet_response()

        result = await svc.query(
            query=_q(timezone_="Not/ATimezone"),
            owner_id=OWNER_ID,
        )
        assert result["timezone"] == "UTC"

    @pytest.mark.asyncio
    async def test_timezone_alias_is_normalised(self):
        """Legacy timezone aliases like Asia/Calcutta -> Asia/Kolkata."""
        svc, click_repo, _ = make_service()
        click_repo.aggregate.return_value = facet_response()

        result = await svc.query(
            query=_q(timezone_="Asia/Calcutta"),
            owner_id=OWNER_ID,
        )
        assert result["timezone"] == "Asia/Kolkata"


# ── Tests: filter query building ─────────────────────────────────────────────


class TestClickQueryBuilding:
    def test_owner_id_filter_always_present(self):
        from bson import ObjectId

        from app.services.stats_service import StatsService

        q = StatsService._build_click_query(OWNER_ID, START, NOW, {})
        assert q["meta.owner_id"] == ObjectId(OWNER_ID)

    def test_time_range_in_query(self):
        from app.services.stats_service import StatsService

        q = StatsService._build_click_query(OWNER_ID, START, NOW, {})
        assert q["clicked_at"]["$gte"] == START
        assert q["clicked_at"]["$lte"] == NOW

    def test_dimension_filter_added(self):
        from app.services.stats_service import StatsService

        q = StatsService._build_click_query(
            OWNER_ID, START, NOW, {"browser": ["Chrome", "Firefox"]}
        )
        assert q["browser"] == {"$in": ["Chrome", "Firefox"]}

    def test_referrer_direct_filter_uses_or_clause(self):
        from app.services.stats_service import StatsService

        q = StatsService._build_click_query(
            OWNER_ID, START, NOW, {"referrer": ["Direct"]}
        )
        assert "$or" in q

    def test_short_code_filter_is_plain(self):
        """No scope lock exists any more — short_code is a plain filter that
        slices the owner-stamped aggregate."""
        from app.services.stats_service import StatsService

        q = StatsService._build_click_query(
            OWNER_ID, START, NOW, {"short_code": ["link1", "link2"]}
        )
        assert q["meta.short_code"] == {"$in": ["link1", "link2"]}

    def test_url_id_filter_cannot_overwrite_locked_url_id(self):
        """url_id filter cannot bypass a per-link lock (security).

        On the per-link path the url_id equality is the only ownership arm,
        so overwriting it would build an ownership-free query."""
        from bson import ObjectId

        from app.services.stats_service import StatsService

        locked = ObjectId()
        q = {"meta.url_id": locked}
        StatsService._apply_dimension_filters(q, {"url_id": [str(ObjectId())]}, [])
        assert q["meta.url_id"] == locked

    def test_plain_utm_filter_added(self):
        from app.services.stats_service import StatsService

        q = StatsService._build_click_query(
            OWNER_ID, START, NOW, {"utm_source": ["newsletter"]}
        )
        assert q["utm_source"] == {"$in": ["newsletter"]}

    def test_utm_none_sentinel_matches_missing_field(self):
        """ "(none)" must match null/missing utm values, like referrer's
        "Direct"."""
        from app.services.stats_service import StatsService

        q = StatsService._build_click_query(
            OWNER_ID, START, NOW, {"utm_source": ["(none)"]}
        )
        assert q["$or"] == [
            {"utm_source": {"$in": ["(none)"]}},
            {"utm_source": {"$in": [None, ""]}},
            {"utm_source": {"$exists": False}},
        ]

    def test_utm_sentinel_mixed_with_values(self):
        from app.services.stats_service import StatsService

        q = StatsService._build_click_query(
            OWNER_ID, START, NOW, {"utm_medium": ["(none)", "email"]}
        )
        assert q["$or"] == [
            {"utm_medium": {"$in": ["(none)", "email"]}},
            {"utm_medium": {"$in": [None, ""]}},
            {"utm_medium": {"$exists": False}},
        ]

    def test_variant_filter_targets_variant_index_as_ints(self):
        from app.services.stats_service import StatsService

        q = StatsService._build_click_query(
            OWNER_ID, START, NOW, {"variant": ["0", "1"]}
        )
        assert q["variant_index"] == {"$in": [0, 1]}
        assert "variant" not in q

    def test_variant_default_sentinel_matches_missing_field(self):
        from app.services.stats_service import StatsService

        q = StatsService._build_click_query(
            OWNER_ID, START, NOW, {"variant": ["(default)", "1"]}
        )
        assert q["$or"] == [
            {"variant_index": {"$in": ["(default)", 1]}},
            {"variant_index": {"$in": [None, ""]}},
            {"variant_index": {"$exists": False}},
        ]

    def test_two_null_sentinel_filters_nest_under_and(self):
        """Two $or groups must combine under $and — a second bare "$or"
        key would silently overwrite the first."""
        from app.services.stats_service import StatsService

        q = StatsService._build_click_query(
            OWNER_ID,
            START,
            NOW,
            {"referrer": ["Direct"], "utm_source": ["(none)"]},
        )
        assert "$or" not in q
        assert len(q["$and"]) == 2
        assert all("$or" in group for group in q["$and"])

    def test_device_filter_added(self):
        from app.services.stats_service import StatsService

        q = StatsService._build_click_query(
            OWNER_ID, START, NOW, {"device": ["mobile", "tablet"]}
        )
        assert q["device"] == {"$in": ["mobile", "tablet"]}

    def test_device_unknown_matches_stored_and_missing(self):
        """ "unknown" is BOTH a stored value (classifier fallback) and the
        sentinel for pre-device-tracking clicks — the filter must match
        both, or it disagrees with what group-by shows."""
        from app.services.stats_service import StatsService

        q = StatsService._build_click_query(
            OWNER_ID, START, NOW, {"device": ["unknown"]}
        )
        assert q["$or"] == [
            {"device": {"$in": ["unknown"]}},
            {"device": {"$in": [None, ""]}},
            {"device": {"$exists": False}},
        ]

    def test_device_groupby_and_filter_agree_on_missing_docs(self):
        """The invariant: a click doc with no device field lands in the
        same "unknown" bucket for group-by (aggregation $ifNull default),
        for filtering (null-sentinel map), and for new writes (classifier
        fallback). If any of the three drifts, widget counts and filter
        counts stop agreeing."""
        from app.services.click.handlers import classify_device
        from app.services.stats_service import _NULL_SENTINEL_FILTERS
        from app.shared.aggregation_strategies import AggregationStrategyFactory

        pipeline = AggregationStrategyFactory.get("device").build_pipeline({})
        group_expr = pipeline[1]["$group"]["_id"]
        assert group_expr == {"$ifNull": ["$device", "unknown"]}

        from ua_parser import parse as ua_parse

        classifier_fallback = classify_device(
            ua_parse("SomeExoticClient/1.0"), "SomeExoticClient/1.0"
        )
        assert classifier_fallback == "unknown"
        assert _NULL_SENTINEL_FILTERS["device"] == "unknown"


class TestClaimedLinksArm:
    """The account query carries claimed links' pre-claim history via a
    url_id arm."""

    @pytest.mark.asyncio
    async def test_empty_claimed_set_keeps_pure_stamp_query(self):
        from bson import ObjectId

        svc, click_repo, url_repo = make_service()
        click_repo.aggregate.return_value = facet_response()
        url_repo.list_claimed_ids.return_value = []

        await svc.query(query=_q(), owner_id=OWNER_ID)

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

        await svc.query(query=_q(), owner_id=OWNER_ID)

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

        from app.services.stats_service import StatsService

        claimed = [ObjectId("f" * 24)]
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 2, 1, tzinfo=timezone.utc)
        query = StatsService._build_click_query(
            OWNER_ID,
            start,
            end,
            {"referrer": ["Direct"]},
            claimed_url_ids=claimed,
        )
        # Two $or groups (ownership + null-sentinel referrer) → $and nesting.
        assert "$or" not in query
        assert len(query["$and"]) == 2
        assert query["$and"][0]["$or"][0] == {"meta.owner_id": ObjectId(OWNER_ID)}


# ── Tests: url_id filter (account scope) ──────────────────────────────────────


class TestUrlIdFilter:
    def test_url_id_filter_builds_in_arm_of_object_ids(self):
        from bson import ObjectId

        from app.services.stats_service import StatsService

        ids = ["a" * 24, "b" * 24]
        q = StatsService._build_click_query(OWNER_ID, START, NOW, {"url_id": ids})
        assert q["meta.url_id"] == {"$in": [ObjectId(v) for v in ids]}

    def test_url_id_filter_keeps_owner_stamp(self):
        """Isolation: the owner stamp stays in the $match, so a foreign
        url_id in the filter simply matches nothing — no ownership check
        needed, no leak possible."""
        from bson import ObjectId

        from app.services.stats_service import StatsService

        foreign = "f" * 24
        q = StatsService._build_click_query(OWNER_ID, START, NOW, {"url_id": [foreign]})
        assert q["meta.owner_id"] == ObjectId(OWNER_ID)
        assert q["meta.url_id"] == {"$in": [ObjectId(foreign)]}

    @pytest.mark.asyncio
    async def test_url_id_param_reaches_match_via_query(self):
        from bson import ObjectId

        svc, click_repo, _ = make_service()
        click_repo.aggregate.return_value = facet_response()
        oid = "a" * 24

        await svc.query(
            query=_q(url_id=oid),
            owner_id=OWNER_ID,
        )
        match = click_repo.aggregate.call_args[0][0][0]["$match"]
        assert match["meta.url_id"] == {"$in": [ObjectId(oid)]}
        assert match["meta.owner_id"] == ObjectId(OWNER_ID)


# ── Tests: per-link query ─────────────────────────────────────────────────────


def make_url_doc(alias="mylink"):
    from bson import ObjectId

    from app.schemas.models.url import UrlV2Doc

    return UrlV2Doc(
        **{
            "_id": ObjectId("e" * 24),
            "alias": alias,
            "owner_id": ObjectId(OWNER_ID),
            "domain": "spoo.me",
            "created_at": NOW,
            "long_url": "https://example.com/long",
            "status": "ACTIVE",
        }
    )


def _lq(**kwargs):
    from app.schemas.dto.requests.stats import LinkStatsQuery

    defaults = {
        "start_date": START_ISO,
        "end_date": NOW_ISO,
        "group_by": "time",
        "metrics": "clicks",
        "timezone": "UTC",
    }
    defaults.update(kwargs)
    return LinkStatsQuery(**defaults)


class TestQueryLink:
    @pytest.mark.asyncio
    async def test_match_scopes_by_url_id_only(self):
        """The $match is url_id + range — no owner arm, no short_code.

        This is also the claimed-link history guarantee: claim-time
        reattribution stamps pre-claim clicks with meta.url_id, so the
        pure url_id match carries the full history without a claimed arm.
        """
        svc, click_repo, url_repo = make_service()
        click_repo.aggregate.return_value = facet_response()
        url = make_url_doc()

        await svc.query_link(_lq(), url)

        match = click_repo.aggregate.call_args[0][0][0]["$match"]
        assert match["meta.url_id"] == url.id
        assert match["clicked_at"] == {"$gte": START, "$lte": NOW}
        assert "meta.owner_id" not in match
        assert "meta.short_code" not in match
        url_repo.list_claimed_ids.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_response_echoes_url_id_and_alias(self):
        svc, click_repo, _ = make_service()
        click_repo.aggregate.return_value = facet_response()
        url = make_url_doc(alias="promo")

        result = await svc.query_link(_lq(), url)

        assert result["url_id"] == str(url.id)
        assert result["alias"] == "promo"
        assert result["scope"] == "all"
        assert "short_code" not in result

    @pytest.mark.asyncio
    async def test_dimension_filters_applied_with_sentinel_semantics(self):
        svc, click_repo, _ = make_service()
        click_repo.aggregate.return_value = facet_response()

        await svc.query_link(_lq(utm_source="(none)", browser="Chrome"), make_url_doc())

        match = click_repo.aggregate.call_args[0][0][0]["$match"]
        assert match["browser"] == {"$in": ["Chrome"]}
        assert match["$or"] == [
            {"utm_source": {"$in": ["(none)"]}},
            {"utm_source": {"$in": [None, ""]}},
            {"utm_source": {"$exists": False}},
        ]

    @pytest.mark.asyncio
    async def test_variant_group_by_groups_on_variant_index(self):
        svc, click_repo, _ = make_service()
        click_repo.aggregate.return_value = facet_response()

        await svc.query_link(_lq(group_by="variant"), make_url_doc())

        facet = click_repo.aggregate.call_args[0][0][1]["$facet"]
        group = next(s["$group"] for s in facet["variant"] if "$group" in s)
        assert group["_id"] == {"$ifNull": ["$variant_index", "(default)"]}

    @pytest.mark.asyncio
    async def test_utm_group_by_allowed(self):
        svc, click_repo, _ = make_service()
        click_repo.aggregate.return_value = facet_response()

        await svc.query_link(_lq(group_by="time,utm_source"), make_url_doc())

        facet = click_repo.aggregate.call_args[0][0][1]["$facet"]
        assert "utm_source" in facet

    @pytest.mark.asyncio
    async def test_window_validation_shared_with_account_query(self):
        svc, _, _ = make_service()

        with pytest.raises(ValidationError, match="date range cannot exceed 90 days"):
            await svc.query_link(
                _lq(start_date=(NOW - timedelta(days=95)).isoformat()),
                make_url_doc(),
            )


# ── Window parity with the public stats service ──────────────────────────────


class TestResolveWindowParity:
    """StatsService._resolve_window and PublicStatsService._resolve_window are
    two implementations of one contract ("the three must not drift") — run
    both over the same inputs and pin equal answers."""

    @staticmethod
    def _both():
        from app.services.public_stats_service import PublicStatsService

        stats, _, _ = make_service()
        public = PublicStatsService(resolver=AsyncMock(), stats_service=stats)
        return stats, public

    @pytest.mark.parametrize(
        ("start_raw", "end_raw"),
        [
            # fully explicit — byte-equal output required
            ("2025-01-01T00:00:00Z", "2025-02-01T00:00:00Z"),
            # end-only: start defaults to end - 7d, still deterministic
            (None, "2025-02-01T00:00:00Z"),
        ],
        ids=["explicit", "end-only"],
    )
    def test_deterministic_windows_equal(self, start_raw, end_raw):
        stats, public = self._both()
        s1 = stats._resolve_window(start_raw, end_raw)
        s2 = public._resolve_window(start_raw, end_raw, "UTC")[:2]
        assert s1 == s2

    @pytest.mark.parametrize(
        ("start_raw", "end_raw"),
        [
            (None, None),  # both default: end=now, start=now-7d
            # end defaults to now — start must be genuinely recent (module NOW
            # is a frozen constant) or the 90-day cap fires first.
            (
                (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),
                None,
            ),
            ("2999-01-01T00:00:00Z", "2999-01-02T00:00:00Z"),  # future-capped
        ],
        ids=["both-default", "start-only", "future-capped"],
    )
    def test_now_dependent_windows_agree(self, start_raw, end_raw):
        # Each implementation samples now() itself; equality holds up to that
        # sampling skew, so pin the pair to within a second of each other.
        stats, public = self._both()
        s1 = stats._resolve_window(start_raw, end_raw)
        s2 = public._resolve_window(start_raw, end_raw, "UTC")[:2]
        for a, b in zip(s1, s2, strict=True):
            assert abs(a - b) < timedelta(seconds=1)

    @pytest.mark.parametrize(
        ("start_raw", "end_raw", "match"),
        [
            ("not-a-date", None, "invalid start_date"),
            (None, "not-a-date", "invalid end_date"),
            ("2025-02-01T00:00:00Z", "2025-01-01T00:00:00Z", "before end_date"),
            ("2024-01-01T00:00:00Z", "2025-01-01T00:00:00Z", "90 days"),
        ],
        ids=["bad-start", "bad-end", "inverted", "too-wide"],
    )
    def test_rejections_agree(self, start_raw, end_raw, match):
        stats, public = self._both()
        with pytest.raises(ValidationError, match=match):
            stats._resolve_window(start_raw, end_raw)
        with pytest.raises(ValidationError, match=match):
            public._resolve_window(start_raw, end_raw, "UTC")
