"""
StatsService — analytics query orchestration.

Handles query building, $facet aggregation execution, and result formatting.
Framework-agnostic: no FastAPI imports.

The route layer is responsible for:
    - JWT/API key resolution → owner_id
    - API key scope verification (stats:read required)
    - Parsing StatsQuery DTO into individual typed values
    - HTTP response construction

This service receives already-parsed parameters and handles all business
logic from there, including date defaults, privacy checks, and the single
$facet MongoDB call.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, available_timezones

from bson import ObjectId

from errors import (
    AuthenticationError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from infrastructure.logging import get_logger
from repositories.click_repository import ClickRepository
from repositories.url_repository import UrlRepository
from schemas.dto.requests.stats import StatsQuery
from schemas.enums.stats import (
    StatsDimension,
    StatsMetric,
    StatsScope,
)
from shared.aggregation_strategies import AggregationStrategyFactory
from shared.datetime_utils import parse_datetime

log = get_logger(__name__)


# Deprecated timezone names → IANA canonical names (loaded once, not per-request)
_TIMEZONE_ALIASES: dict[str, str] = {
    "Asia/Calcutta": "Asia/Kolkata",
    "Asia/Katmandu": "Asia/Kathmandu",
    "Asia/Rangoon": "Asia/Yangon",
    "Asia/Saigon": "Asia/Ho_Chi_Minh",
    "US/Eastern": "America/New_York",
    "US/Central": "America/Chicago",
    "US/Mountain": "America/Denver",
    "US/Pacific": "America/Los_Angeles",
}

# IANA name snapshot — available_timezones() rescans the tzdata files on
# every call, far too heavy to rebuild per request. Membership checks use
# this set instead.
_KNOWN_TIMEZONES: frozenset[str] = frozenset(available_timezones())

# Dimensions whose aggregation output maps null/missing to a sentinel value
# ("Direct" referrers, "(none)" for untagged utm clicks, "unknown" for
# clicks that predate device tracking). Filtering by the sentinel must
# match those null/missing documents too. Note "unknown" is ALSO a real
# stored value (the classifier's fallback) — the filter branch keeps the
# sentinel in its stored-value $in for exactly that reason.
_NULL_SENTINEL_FILTERS: dict[StatsDimension, str] = {
    StatsDimension.REFERRER: "Direct",
    StatsDimension.DEVICE: "unknown",
    StatsDimension.DOMAIN: "unknown",
    StatsDimension.UTM_SOURCE: "(none)",
    StatsDimension.UTM_MEDIUM: "(none)",
    StatsDimension.UTM_CAMPAIGN: "(none)",
}

# Dimensions whose clicks field lives under the time-series metaField rather
# than at the top level. short_code is not listed — it has its own branch
# (scope-bypass prevention).
_FILTER_FIELD_PATHS: dict[StatsDimension, str] = {
    StatsDimension.DOMAIN: "meta.domain",
}


class StatsService:
    """Analytics query service.

    Args:
        click_repo:         Repository for the ``clicks`` time-series collection.
        url_repo:           Repository for the ``urlsV2`` collection (privacy checks).
        max_date_range_days: Maximum allowed date range in days (default 90).
    """

    def __init__(
        self,
        click_repo: ClickRepository,
        url_repo: UrlRepository,
        max_date_range_days: int = 90,
    ) -> None:
        self._max_date_range_days = max_date_range_days
        self._click_repo = click_repo
        self._url_repo = url_repo

    # ── Timezone helpers (shared with the public stats endpoint) ─────────────

    @staticmethod
    def to_user_tz(dt: datetime | None, tz_name: str) -> datetime | None:
        """Convert a UTC datetime to the user's timezone."""
        if dt is None:
            return None
        try:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(ZoneInfo(tz_name))
        except Exception as exc:
            log.warning(
                "timezone_conversion_failed",
                timezone=tz_name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return dt

    @staticmethod
    def normalize_timezone(tz_name: str) -> str:
        """Map deprecated aliases to canonical IANA names; unknown → UTC.

        Shared by ``query()`` and the public per-link stats endpoint so
        both normalize identically (and neither rescans tzdata per call).
        """
        tz_name = _TIMEZONE_ALIASES.get(tz_name, tz_name)
        if tz_name not in _KNOWN_TIMEZONES:
            log.info("invalid_timezone_provided", timezone=tz_name, fallback="UTC")
            tz_name = "UTC"
        return tz_name

    @staticmethod
    def _fmt_tz(dt: datetime | None, tz_name: str) -> datetime | None:
        """Convert a UTC datetime to the user's timezone."""
        return StatsService.to_user_tz(dt, tz_name)

    # ── Private: query building ───────────────────────────────────────────────

    @staticmethod
    def _build_click_query(
        scope: StatsScope,
        owner_id: str | None,
        short_code: str | None,
        start_date: datetime,
        end_date: datetime,
        filters: dict[str, list[str]],
        claimed_url_ids: list[ObjectId] | None = None,
    ) -> dict[str, Any]:
        """Build the MongoDB $match query for the clicks collection.

        Preserves the exact filtering logic from utils/query_builder.py
        (ClickQueryBuilder + StatsQueryBuilderFactory), including:
        - meta.owner_id scoping for scope=all
        - meta.short_code scoping for scope=anon
        - Time range on clicked_at
        - Dimension filters with special handling for short_code and referrer/Direct

        ``claimed_url_ids`` (scope=all only): claimed links' pre-claim
        clicks are stamped with the anonymous owner, so they need a url_id
        arm to carry their history. Empty set (almost every account) →
        query byte-identical to the stamp-only one.
        """
        query: dict[str, Any] = {}

        # Ownership scope and null-sentinel filters both build $or groups;
        # multiple groups must nest under $and.
        or_groups: list[list[dict[str, Any]]] = []

        # Scope filter
        if scope == StatsScope.ALL and owner_id:
            owner_oid = ObjectId(owner_id) if isinstance(owner_id, str) else owner_id
            if claimed_url_ids:
                or_groups.append(
                    [
                        {"meta.owner_id": owner_oid},
                        {"meta.url_id": {"$in": claimed_url_ids}},
                    ]
                )
            else:
                query["meta.owner_id"] = owner_oid
        elif scope == StatsScope.ANON and short_code:
            query["meta.short_code"] = short_code

        # Time range
        query["clicked_at"] = {"$gte": start_date, "$lte": end_date}

        # Dimension filters (null-sentinel dimensions add $or groups too).
        for dimension, values in filters.items():
            if not values:
                continue

            if dimension == StatsDimension.SHORT_CODE:
                # SECURITY: skip if scope already locks short_code (scope=anon)
                if "meta.short_code" in query:
                    log.warning(
                        "query_builder_scope_bypass_prevented",
                        dimension="short_code",
                        locked_short_code=query.get("meta.short_code"),
                        attempted_values=values,
                    )
                    continue
                query["meta.short_code"] = {"$in": values}
            elif (
                dimension in _NULL_SENTINEL_FILTERS
                and _NULL_SENTINEL_FILTERS[dimension] in values
            ):
                # The stored-value $in keeps the sentinel: for device,
                # "unknown" is also a real stored value; for referrer/utm/
                # domain the extra literal matches nothing (and if a stored
                # value ever equalled it, group-by merges it with null anyway).
                field = _FILTER_FIELD_PATHS.get(dimension, dimension)
                or_groups.append(
                    [
                        {field: {"$in": values}},
                        {field: {"$in": [None, ""]}},
                        {field: {"$exists": False}},
                    ]
                )
            else:
                query[_FILTER_FIELD_PATHS.get(dimension, dimension)] = {"$in": values}

        if len(or_groups) == 1:
            query["$or"] = or_groups[0]
        elif or_groups:
            query["$and"] = [{"$or": group} for group in or_groups]

        return query

    # ── Private: aggregation ──────────────────────────────────────────────────

    async def _execute_all_stats(
        self,
        query: dict[str, Any],
        group_by: list[str],
        start_date: datetime,
        end_date: datetime,
        tz_name: str,
    ) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
        """Execute summary + all dimension aggregations in ONE $facet DB call.

        This is critical for serverless (Vercel) where each DB round-trip adds
        ~200-400 ms of latency.  $facet runs all pipelines in parallel on the
        MongoDB server.

        Returns:
            (summary_stats, dimension_results)
        """
        # Build per-dimension strategies
        strategies: dict[str, Any] = {}
        for dim in group_by:
            if dim == StatsDimension.TIME:
                strategies[dim] = AggregationStrategyFactory.get(
                    dim,
                    start_date=start_date,
                    end_date=end_date,
                    timezone=tz_name,
                )
            else:
                strategies[dim] = AggregationStrategyFactory.get(dim)

        # $facet stages — each dimension aggregation skips the leading $match
        # because we apply $match once at the combined pipeline level.
        facet_stages: dict[str, Any] = {}

        # Summary facet: total clicks, unique IPs, first/last click, avg redirect
        facet_stages["_summary"] = [
            {
                "$group": {
                    "_id": None,
                    "total_clicks": {"$sum": 1},
                    "unique_clicks": {"$addToSet": "$ip_address"},
                    "first_click": {"$min": "$clicked_at"},
                    "last_click": {"$max": "$clicked_at"},
                    "avg_redirection_time": {"$avg": "$redirect_ms"},
                }
            },
            {"$addFields": {"unique_clicks": {"$size": "$unique_clicks"}}},
        ]

        for dim, strategy in strategies.items():
            full_pipeline = strategy.build_pipeline(query)
            # Strip the leading $match — we apply it once at the top
            facet_stages[dim] = full_pipeline[1:] if full_pipeline else []

        combined_pipeline = [
            {"$match": query},
            {"$facet": facet_stages},
        ]

        # Default empty summary. avg_redirection_time is None, not 0 —
        # "no measurements in range" must never read as an instant redirect.
        summary: dict[str, Any] = {
            "total_clicks": 0,
            "unique_clicks": 0,
            "first_click": None,
            "last_click": None,
            "avg_redirection_time": None,
        }
        results: dict[str, list[dict[str, Any]]] = {}

        try:
            raw = await self._click_repo.aggregate(combined_pipeline)

            if raw:
                facet_results = raw[0]

                # Extract summary
                summary_list = facet_results.get("_summary", [])
                if summary_list:
                    s = summary_list[0]
                    # $avg yields None when no click in range carried a
                    # redirect_ms measurement — pass it through as null.
                    avg_redirect = s.get("avg_redirection_time")
                    summary = {
                        "total_clicks": s.get("total_clicks", 0),
                        "unique_clicks": s.get("unique_clicks", 0),
                        "first_click": self._fmt_tz(s.get("first_click"), tz_name),
                        "last_click": self._fmt_tz(s.get("last_click"), tz_name),
                        "avg_redirection_time": (
                            round(avg_redirect, 2) if avg_redirect is not None else None
                        ),
                    }

                # Extract per-dimension results
                for dim, strategy in strategies.items():
                    results[dim] = strategy.format_results(facet_results.get(dim, []))
            else:
                for dim in group_by:
                    results[dim] = []

        except Exception as exc:
            log.error(
                "stats_facet_aggregation_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                dimensions=group_by,
            )
            for dim in group_by:
                results[dim] = []

        return summary, results

    # ── Response formatting ──────────────────────────────────────────────────

    def _format_results(
        self,
        scope: StatsScope,
        short_code: str | None,
        start_date: datetime,
        end_date: datetime,
        filters: dict[str, list[str]],
        group_by: list[str],
        metrics: list[str],
        tz_name: str,
        aggregation_results: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Format aggregation results into the API response structure.

        Preserves the exact shape from StatsQueryBuilder._format_results().
        """
        response: dict[str, Any] = {
            "scope": scope,
            "filters": filters,
            "group_by": group_by,
            "timezone": tz_name,
            "metrics": {},
        }

        if scope == StatsScope.ANON:
            response["short_code"] = short_code

        response["time_range"] = {
            "start_date": self._fmt_tz(start_date, tz_name),
            "end_date": self._fmt_tz(end_date, tz_name),
        }

        # Add time bucket info when time aggregation is requested
        if (
            StatsDimension.TIME in group_by
            and StatsDimension.TIME in aggregation_results
        ):
            try:
                time_strategy = AggregationStrategyFactory.get(
                    "time",
                    start_date=start_date,
                    end_date=end_date,
                    timezone=tz_name,
                )
                if hasattr(time_strategy, "get_bucket_info"):
                    response["time_bucket_info"] = time_strategy.get_bucket_info()
            except Exception as exc:
                log.warning(
                    "time_bucket_info_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

        # Build metrics dict
        for dim, dim_results in aggregation_results.items():
            for metric in metrics:
                result_key = "total_clicks" if metric == StatsMetric.CLICKS else metric
                metric_key = f"{metric}_by_{dim}"
                response["metrics"][metric_key] = []
                for result in dim_results:
                    if dim == StatsDimension.TIME:
                        dim_value = result.get("date", "unknown")
                    elif dim == StatsDimension.SHORT_CODE:
                        dim_value = result.get(
                            "short_code", result.get("alias", "unknown")
                        )
                    else:
                        dim_value = result.get(dim, "unknown")
                    response["metrics"][metric_key].append(
                        {dim: dim_value, metric: result.get(result_key, 0)}
                    )

        return response

    @staticmethod
    def add_metadata(response: dict[str, Any]) -> dict[str, Any]:
        """Enhance the response with metadata and computed percentages.

        Preserves the exact logic from utils/stats_utils.format_stats_response_with_metadata().
        Public: the v1 branch of the public stats endpoint decorates its
        synthesized wire with this too, so percentages/computed_metrics
        can never drift between generations.
        """
        response = response.copy()

        response["generated_at"] = datetime.now(timezone.utc)
        response["api_version"] = "v1"

        summary = response.get("summary", {})
        if summary:
            total_clicks = summary.get("total_clicks", 0)
            unique_clicks = summary.get("unique_clicks", 0)
            if total_clicks > 0:
                unique_rate = (unique_clicks / total_clicks) * 100
                response["computed_metrics"] = {
                    "unique_click_rate": round(unique_rate, 2),
                    "repeat_click_rate": round(100 - unique_rate, 2),
                    "average_clicks_per_visitor": round(
                        total_clicks / unique_clicks if unique_clicks > 0 else 0, 2
                    ),
                }

        # Add percentage fields to each metric dimension
        metrics = response.get("metrics", {})
        for _metric_key, metric_data in metrics.items():
            if isinstance(metric_data, list) and metric_data:
                total = sum(
                    item.get(list(item.keys())[-1], 0)
                    for item in metric_data
                    if isinstance(item, dict)
                )
                if total > 0:
                    for item in metric_data:
                        if isinstance(item, dict):
                            value_key = list(item.keys())[-1]
                            value = item.get(value_key, 0)
                            item[f"{value_key}_percentage"] = round(
                                (value / total) * 100, 2
                            )

        return response

    # ── Public API ────────────────────────────────────────────────────────────

    async def compute(
        self,
        click_query: dict[str, Any],
        *,
        scope: StatsScope,
        short_code: str | None,
        start_date: datetime,
        end_date: datetime,
        group_by: list[str],
        metrics: list[str],
        tz_name: str,
        filters: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        """Run a pre-built clicks ``$match`` and format the standard wire.

        The single public seam over the aggregation chain ($facet
        execution → result formatting → summary → metadata). Both
        ``query()`` and the public per-link stats endpoint go through
        this, so the wire shape can never drift between the dashboard
        and the public page.

        The caller owns access control and scoping: ``click_query``
        arrives fully built (owner-scoped, url_id-scoped, ...) with
        already-validated dates and a normalized timezone.
        """
        summary, aggregation_results = await self._execute_all_stats(
            click_query, group_by, start_date, end_date, tz_name
        )
        response = self._format_results(
            scope,
            short_code,
            start_date,
            end_date,
            filters or {},
            group_by,
            metrics,
            tz_name,
            aggregation_results,
        )
        response["summary"] = summary
        return self.add_metadata(response)

    async def query(
        self,
        query: StatsQuery,
        owner_id: str | None,
    ) -> dict[str, Any]:
        """Execute a stats query and return the formatted, enhanced response.

        Args:
            query:    Validated StatsQuery DTO with all query parameters.
            owner_id: String user ID for scope=all, or None.

        Returns:
            Formatted stats dict ready for JSON serialisation.

        Raises:
            ValidationError:      Invalid scope/target parameters.
            NotFoundError:        short_code not found (scope=anon).
            AuthenticationError:  Auth required for private stats or scope=all.
            ForbiddenError:       Authenticated user does not own private URL.
            AppError:             DB failure.
        """
        scope = query.scope
        short_code = query.short_code
        start_date = parse_datetime(query.start_date) if query.start_date else None
        end_date = parse_datetime(query.end_date) if query.end_date else None

        if query.start_date and start_date is None:
            raise ValidationError("invalid start_date format")
        if query.end_date and end_date is None:
            raise ValidationError("invalid end_date format")
        filters = query.parsed_filters
        group_by = query.parsed_group_by
        metrics = query.parsed_metrics
        tz_name = query.timezone

        start_time = time.perf_counter()

        # ── Apply date defaults ───────────────────────────────────────────────
        now = datetime.now(timezone.utc)
        if start_date is None and end_date is None:
            end_date = now
            start_date = now - timedelta(days=7)
        elif start_date is None:
            start_date = end_date - timedelta(days=7)
        elif end_date is None:
            end_date = now

        # Cap future dates to now
        if start_date > now:
            start_date = now
        if end_date > now:
            end_date = now

        # ── Date range validation ─────────────────────────────────────────────
        if start_date > end_date:
            raise ValidationError("start_date must be before end_date")
        if (end_date - start_date).days > self._max_date_range_days:
            raise ValidationError(
                f"date range cannot exceed {self._max_date_range_days} days"
            )

        # ── Validate timezone ─────────────────────────────────────────────────
        tz_name = self.normalize_timezone(tz_name)

        # ── Scope/target validation ───────────────────────────────────────────
        if scope == StatsScope.ANON:
            if not short_code:
                raise ValidationError("short_code is required when scope=anon")

            privacy = await self._url_repo.check_stats_privacy(short_code)
            if not privacy["exists"]:
                raise NotFoundError("short_code not found")

            if privacy["private"]:
                if owner_id is None:
                    log.info(
                        "stats_access_denied",
                        reason="unauthenticated_private_stats",
                        short_code=short_code,
                    )
                    raise AuthenticationError(
                        "this URL has private statistics - authentication required"
                    )
                if privacy["owner_id"] != str(owner_id):
                    log.info(
                        "stats_access_denied",
                        reason="not_owner",
                        short_code=short_code,
                        requesting_user=str(owner_id),
                        owner_user=privacy["owner_id"],
                    )
                    raise ForbiddenError("access denied - private statistics")

        elif scope == StatsScope.ALL:
            if owner_id is None:
                log.info(
                    "stats_access_denied",
                    reason="unauthenticated_scope_all",
                )
                raise AuthenticationError("authentication required for scope=all")

        # ── Execute + format ──────────────────────────────────────────────────
        # Claimed links carry pre-claim history under the anonymous stamp;
        # sub-ms partial-index lookup, empty for almost every account.
        claimed_url_ids: list[ObjectId] = []
        if scope == StatsScope.ALL and owner_id:
            claimed_url_ids = await self._url_repo.list_claimed_ids(
                ObjectId(owner_id) if isinstance(owner_id, str) else owner_id
            )

        click_query = self._build_click_query(
            scope,
            owner_id,
            short_code,
            start_date,
            end_date,
            filters,
            claimed_url_ids=claimed_url_ids,
        )
        response = await self.compute(
            click_query,
            scope=scope,
            short_code=short_code,
            start_date=start_date,
            end_date=end_date,
            group_by=group_by,
            metrics=metrics,
            tz_name=tz_name,
            filters=filters,
        )
        summary = response.get("summary", {})

        duration_ms = int((time.perf_counter() - start_time) * 1000)
        log.info(
            "stats_query",
            scope=scope,
            short_code=short_code if scope == StatsScope.ANON else None,
            group_by=group_by,
            metrics=metrics,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            filter_count=len(filters),
            total_clicks=summary.get("total_clicks", 0),
            unique_clicks=summary.get("unique_clicks", 0),
            duration_ms=duration_ms,
        )

        return response
