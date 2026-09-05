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
logic from there, including date defaults and the single $facet MongoDB
call.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, available_timezones

from bson import ObjectId

from errors import (
    AuthenticationError,
    ValidationError,
)
from infrastructure.logging import get_logger
from repositories.click_repository import ClickRepository
from repositories.url_repository import UrlRepository

if TYPE_CHECKING:
    from services.tag_service import TagService
from schemas.dto.requests.stats import LinkStatsQuery, StatsQuery
from schemas.enums.stats import (
    StatsDimension,
    StatsMetric,
    StatsScope,
)
from schemas.models.url import UrlV2Doc
from services.features.catalog import UNLIMITED
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
# A valid-shaped ObjectId no document can hold: an empty tag scope must
# match nothing, and $in: [] would be dropped by the "no values" guard.
_NO_URL_ID = "000000000000000000000000"

_NULL_SENTINEL_FILTERS: dict[StatsDimension, str] = {
    StatsDimension.REFERRER: "Direct",
    StatsDimension.DEVICE: "unknown",
    StatsDimension.UTM_SOURCE: "(none)",
    StatsDimension.UTM_MEDIUM: "(none)",
    StatsDimension.UTM_CAMPAIGN: "(none)",
    StatsDimension.VARIANT: "(default)",
}

# Wire dimension → stored field, where the two differ.
_DIMENSION_FIELDS: dict[StatsDimension, str] = {
    StatsDimension.VARIANT: "variant_index",
}


def _stored_values(dimension: str, values: list[str]) -> list[Any]:
    """Wire filter values in the stored type: variant indices are ints."""
    if dimension == StatsDimension.VARIANT:
        return [int(v) if v.isdigit() else v for v in values]
    return values


class StatsService:
    """Analytics query service.

    Args:
        click_repo:         Repository for the ``clicks`` time-series collection.
        url_repo:           Repository for the ``urlsV2`` collection (claimed ids).
    """

    def __init__(
        self,
        click_repo: ClickRepository,
        url_repo: UrlRepository,
        tag_service: TagService | None = None,
    ) -> None:
        self._click_repo = click_repo
        self._url_repo = url_repo
        self._tag_service = tag_service

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

    # ── Private: date window ──────────────────────────────────────────────────

    def resolve_window(
        self,
        start_raw: str | None,
        end_raw: str | None,
        window_days: int,
    ) -> tuple[datetime, datetime, bool]:
        """Parse, default, cap, and clamp the date window.

        ``window_days`` is the caller's plan retention window (``-1`` means
        unlimited). A range reaching further back is clamped to the window
        and the third value says so, so the response can name it. Shared by
        ``query()``, ``query_link()`` and the public stats page.
        """
        start_date = parse_datetime(start_raw) if start_raw else None
        if start_raw and start_date is None:
            raise ValidationError("invalid start_date format")
        end_date = parse_datetime(end_raw) if end_raw else None
        if end_raw and end_date is None:
            raise ValidationError("invalid end_date format")

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

        if start_date > end_date:
            raise ValidationError("start_date must be before end_date")
        clamped = False
        if window_days != UNLIMITED and end_date - start_date > timedelta(
            days=window_days
        ):
            start_date = end_date - timedelta(days=window_days)
            clamped = True

        return start_date, end_date, clamped

    # ── Private: query building ───────────────────────────────────────────────

    @staticmethod
    def _build_click_query(
        owner_id: str,
        start_date: datetime,
        end_date: datetime,
        filters: dict[str, list[str]],
        claimed_url_ids: list[ObjectId] | None = None,
    ) -> dict[str, Any]:
        """Build the MongoDB $match query for the clicks collection.

        Always owner-scoped (``meta.owner_id``) — auth is mandatory on
        every caller. Time range on ``clicked_at``, then dimension filters.

        ``claimed_url_ids``: claimed links' pre-claim clicks are stamped
        with the anonymous owner, so they need a url_id arm to carry their
        history. Empty set (almost every account) → query byte-identical
        to the stamp-only one.
        """
        query: dict[str, Any] = {}

        # Ownership and null-sentinel filters both build $or groups;
        # multiple groups must nest under $and.
        or_groups: list[list[dict[str, Any]]] = []

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

        # Time range
        query["clicked_at"] = {"$gte": start_date, "$lte": end_date}

        StatsService._apply_dimension_filters(query, filters, or_groups)
        StatsService._merge_or_groups(query, or_groups)

        return query

    async def _resolve_tag_filter(
        self, filters: dict[str, list[str]], owner_oid: ObjectId
    ) -> dict[str, list[str]]:
        """Swap ``tag``/``tag_id`` filters for the url_ids of the owner's links.

        Clicks never carry tags (a click's meta is frozen at write time, a
        link's tags are not), so the scope is resolved through the link
        collection. Names resolve through the tag registry; ids are taken
        as given (foreign ids own no links, so they match nothing).
        Intersects with an explicit ``url_id`` filter; a tag nobody uses
        resolves to the ``_NO_URL_ID`` sentinel so the $match finds nothing
        instead of everything.
        """
        names = filters.get(StatsDimension.TAG) or []
        raw_ids = filters.get(StatsDimension.TAG_ID) or []
        if not names and not raw_ids:
            return filters
        resolved = dict(filters)
        resolved.pop(StatsDimension.TAG, None)
        resolved.pop(StatsDimension.TAG_ID, None)
        tag_ids = [ObjectId(v) for v in raw_ids]
        if names and self._tag_service is not None:
            named = await self._tag_service.ids_for_names(owner_oid, names)
            tag_ids += [i for i in named if i not in tag_ids]
        ids: set[str] = set()
        if tag_ids:
            ids = {
                str(i)
                for i in await self._url_repo.list_ids_by_owner_and_tag_ids(
                    owner_oid, tag_ids
                )
            }
        explicit = resolved.get(StatsDimension.URL_ID)
        if explicit:
            ids &= set(explicit)
        resolved[StatsDimension.URL_ID] = sorted(ids) or [_NO_URL_ID]
        return resolved

    @staticmethod
    def _apply_dimension_filters(
        query: dict[str, Any],
        filters: dict[str, list[str]],
        or_groups: list[list[dict[str, Any]]],
    ) -> None:
        """Apply dimension filters to a partially built $match in place.

        Null-sentinel dimensions append $or groups instead of plain arms —
        the caller merges them via ``_merge_or_groups``. Shared by the
        account-scope builder and the per-link query.
        """
        for dimension, values in filters.items():
            if not values:
                continue

            if dimension == StatsDimension.SHORT_CODE:
                query["meta.short_code"] = {"$in": values}
            elif dimension == StatsDimension.URL_ID:
                # SECURITY: skip if the query already locks url_id — on the
                # per-link path that equality is the only ownership arm.
                if "meta.url_id" in query:
                    log.warning(
                        "query_builder_scope_bypass_prevented",
                        dimension="url_id",
                        attempted_values=values,
                    )
                    continue
                # Values are format-validated by the DTO. No ownership check:
                # the owner stamp already scopes the $match, so foreign ids
                # simply match nothing.
                query["meta.url_id"] = {"$in": [ObjectId(v) for v in values]}
            elif (
                dimension in _NULL_SENTINEL_FILTERS
                and _NULL_SENTINEL_FILTERS[dimension] in values
            ):
                # The stored-value $in keeps the sentinel: for device,
                # "unknown" is also a real stored value; for referrer/utm
                # the extra literal matches nothing (and if a visitor ever
                # sends the literal, group-by merges it with null anyway).
                field = _DIMENSION_FIELDS.get(dimension, dimension)
                or_groups.append(
                    [
                        {field: {"$in": _stored_values(dimension, values)}},
                        {field: {"$in": [None, ""]}},
                        {field: {"$exists": False}},
                    ]
                )
            else:
                field = _DIMENSION_FIELDS.get(dimension, dimension)
                query[field] = {"$in": _stored_values(dimension, values)}

    @staticmethod
    def _merge_or_groups(
        query: dict[str, Any],
        or_groups: list[list[dict[str, Any]]],
    ) -> None:
        """Fold accumulated $or groups into the $match (nesting under $and)."""
        if len(or_groups) == 1:
            query["$or"] = or_groups[0]
        elif or_groups:
            query["$and"] = [{"$or": group} for group in or_groups]

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
        clamped: bool = False,
        window_days: int | None = None,
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
            "clamped": clamped,
            "window_days": window_days,
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
        clamped: bool = False,
        window_days: int | None = None,
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
            clamped=clamped,
            window_days=window_days,
        )
        response["summary"] = summary
        return self.add_metadata(response)

    async def query(
        self,
        query: StatsQuery,
        owner_id: str | None,
        *,
        window_days: int,
    ) -> dict[str, Any]:
        """Execute an account-scoped stats query and return the enhanced response.

        Args:
            query:    Validated StatsQuery DTO with all query parameters.
            owner_id: String user ID of the authenticated caller.

        Returns:
            Formatted stats dict ready for JSON serialisation.

        Raises:
            ValidationError:      Invalid date parameters.
            AuthenticationError:  No owner_id (defence in depth — the route
                                  already requires auth).
            AppError:             DB failure.
        """
        filters = query.parsed_filters
        group_by = query.parsed_group_by
        metrics = query.parsed_metrics

        start_time = time.perf_counter()

        start_date, end_date, clamped = self.resolve_window(
            query.start_date, query.end_date, window_days
        )
        tz_name = self.normalize_timezone(query.timezone)

        if owner_id is None:
            log.info("stats_access_denied", reason="unauthenticated")
            raise AuthenticationError("authentication required")

        # ── Execute + format ──────────────────────────────────────────────────
        # Claimed links carry pre-claim history under the anonymous stamp;
        # sub-ms partial-index lookup, empty for almost every account.
        owner_oid = ObjectId(owner_id) if isinstance(owner_id, str) else owner_id
        claimed_url_ids = await self._url_repo.list_claimed_ids(owner_oid)

        click_query = self._build_click_query(
            owner_id,
            start_date,
            end_date,
            await self._resolve_tag_filter(filters, owner_oid),
            claimed_url_ids=claimed_url_ids,
        )
        response = await self.compute(
            click_query,
            scope=StatsScope.ALL,
            short_code=None,
            start_date=start_date,
            end_date=end_date,
            group_by=group_by,
            metrics=metrics,
            tz_name=tz_name,
            filters=filters,
            clamped=clamped,
            window_days=window_days,
        )
        summary = response.get("summary", {})

        duration_ms = int((time.perf_counter() - start_time) * 1000)
        log.info(
            "stats_query",
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

    async def query_link(
        self,
        query: LinkStatsQuery,
        url: UrlV2Doc,
        *,
        window_days: int,
    ) -> dict[str, Any]:
        """Execute a per-link stats query for an already-resolved owned URL.

        The route layer owns resolution and access control (parse_url_id →
        get_owned, so a foreign id answers exactly like a missing one).
        Clicks match on ``meta.url_id`` — never ``meta.short_code``, so a
        same-alias link on another domain can never bleed in. Claimed links
        need no special arm: claim-time reattribution stamps pre-claim
        clicks with ``meta.url_id``, so this match carries full history.

        Args:
            query: Validated LinkStatsQuery DTO.
            url:   The owned URL document resolved by the route.

        Returns:
            The standard stats wire plus top-level ``url_id`` and ``alias``.

        Raises:
            ValidationError: Invalid dates / range too large.
            AppError:        DB failure.
        """
        filters = query.parsed_filters
        group_by = query.parsed_group_by
        metrics = query.parsed_metrics

        start_time = time.perf_counter()

        start_date, end_date, clamped = self.resolve_window(
            query.start_date, query.end_date, window_days
        )
        tz_name = self.normalize_timezone(query.timezone)

        click_query: dict[str, Any] = {
            "meta.url_id": url.id,
            "clicked_at": {"$gte": start_date, "$lte": end_date},
        }
        or_groups: list[list[dict[str, Any]]] = []
        self._apply_dimension_filters(click_query, filters, or_groups)
        self._merge_or_groups(click_query, or_groups)

        response = await self.compute(
            click_query,
            scope=StatsScope.ALL,
            short_code=None,
            start_date=start_date,
            end_date=end_date,
            group_by=group_by,
            metrics=metrics,
            tz_name=tz_name,
            filters=filters,
            clamped=clamped,
            window_days=window_days,
        )
        response["url_id"] = str(url.id)
        response["alias"] = url.alias
        summary = response.get("summary", {})

        duration_ms = int((time.perf_counter() - start_time) * 1000)
        log.info(
            "stats_query",
            url_id=str(url.id),
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
