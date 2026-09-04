"""
PublicStatsService — public per-link statistics for the /stats/{code} page.

Resolves a short code across BOTH URL generations and returns link facts
plus the modern stats wire shape (identical keys to GET /api/v1/stats).

Resolution, raw v1/emoji reads, derived effective status, and created_at
all come from ``services.public_link_resolver`` — the single source of
truth shared with the public preview endpoint, so the two public surfaces
can never disagree about which link a code names or what state it is in.

Privacy semantics (frozen contract):
  - a v2 link with private stats answers BYTE-IDENTICALLY to a missing
    code (no oracle distinguishing "private" from "absent");
  - password-protected links answer 401 ``password_required`` until the
    password arrives in a POST body; a wrong password is 401
    ``invalid_password``; a password never rides a URL;
  - an authenticated owner bypasses both the privacy and password gates.

v2 analytics reuse StatsService's aggregation machinery scoped by
``meta.url_id`` (never ``meta.short_code`` — a same-alias link on a custom
domain must not bleed in). v1/emoji analytics are synthesized into the
same wire shape from the embedded document counters: dimensions are
LIFETIME totals (that's all v1 stores), only the daily time series is
windowed.

Framework-agnostic: no FastAPI imports. Status is derived READ-ONLY —
this service never writes.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from bson import ObjectId

from errors import (
    InvalidPasswordError,
    NotFoundError,
    PasswordRequiredError,
)
from infrastructure.crypto import verify_password
from infrastructure.logging import get_logger
from schemas.enums.stats import StatsScope
from schemas.models.base import ANONYMOUS_OWNER_ID
from schemas.models.url import LegacyUrlDoc, UrlV2Doc
from services.entitlements import EntitlementService
from services.feature_flag_service import DOMAIN_POLISH_FLAG, FeatureFlagService
from services.features.catalog import Plan, plan_defaults
from services.public_link_resolver import PublicLinkResolver, ResolvedPublicLink
from services.stats_service import StatsService
from shared.aggregation_strategies import convert_country_name

log = get_logger(__name__)

_PublicDoc = UrlV2Doc | LegacyUrlDoc

# Dimensions are FIXED per generation (no group_by on the wire). v1 never
# emits city or device (not stored); v2 never emits bots (tracked
# per-click, not as a dimension the public page shows). utm_* dimensions
# are deliberately absent from BOTH: campaign tagging is the owner's
# marketing data, never public.
_V2_GROUP_BY = ["time", "browser", "os", "device", "country", "city", "referrer"]
_V1_GROUP_BY = ["time", "browser", "os", "country", "referrer"]
_METRICS = ["clicks", "unique_clicks"]

_V1_LAST_CLICK_FORMAT = "%Y-%m-%d %H:%M:%S"


class PublicStatsService:
    """Public stats query service.

    Args:
        resolver:            The shared public-link resolver (resolution,
                             raw v1 reads, derived status, created_at).
        stats_service:       The shared StatsService (v2 aggregation reuse).
        entitlements:        Resolves the owner's plan: the analytics window
                             the page may show and whether it is whitelabel.
    """

    def __init__(
        self,
        resolver: PublicLinkResolver,
        stats_service: StatsService,
        entitlements: EntitlementService,
        flags: FeatureFlagService,
    ) -> None:
        self._resolver = resolver
        self._stats = stats_service
        self._entitlements = entitlements
        self._flags = flags

    # ── Public API ────────────────────────────────────────────────────────────

    async def get_public_stats(
        self,
        short_code: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        tz_name: str = "UTC",
        password: str | None = None,
        user_id: ObjectId | None = None,
    ) -> dict[str, Any]:
        """Resolve a short code and return its public stats payload.

        Args:
            short_code: Path alias (may arrive percent-encoded — emoji).
            start_date: ISO datetime string; defaults to end_date - 7 days.
            end_date:   ISO datetime string; defaults to now.
            tz_name:    IANA timezone for bucketing/formatting.
            password:   Password from a POST body (never from a URL).
            user_id:    Authenticated user id, if any (owner bypass).

        Returns:
            ``{"generation", "link", "stats"}`` ready for the response DTO.

        Raises:
            NotFoundError:         Unknown code — or private stats
                                   (byte-identical body, no oracle).
            PasswordRequiredError: Password set, none supplied (401).
            InvalidPasswordError:  Password set, wrong one supplied (401).
            ValidationError:       Bad dates / range too large.

        Note: the privacy gate only applies to v2 — legacy docs have no
        privacy concept (no ``private_stats`` field exists), so v1/emoji
        stats are public BY DESIGN, intentional parity with the shipped
        legacy /stats page.
        """
        started = time.perf_counter()

        link = await self._resolver.resolve(short_code)
        if link is None:
            raise NotFoundError("short_code not found")

        is_v2 = link.is_v2
        # The resolver keeps v1 docs RAW (legacy-only fields survive for
        # created_at); typed access (parsed datetimes, int coercion, map
        # defaults) drives the gates and the synthesis.
        doc: _PublicDoc = link.v2_doc if is_v2 else LegacyUrlDoc.from_mongo(link.raw_v1)

        # Owner bypass rides the same request. The anonymous sentinel
        # owner_id can never equal a real user id, so no special-casing.
        is_owner = bool(is_v2 and user_id is not None and doc.owner_id == user_id)

        # Private stats answer exactly like a missing code. Semantics:
        # True = private (owned default), None = anonymous/unowned = public,
        # False = explicitly public. v1/emoji have NO privacy concept (the
        # legacy schema never grew a private_stats field) — public by
        # design, mirroring the shipped legacy /stats page.
        if is_v2 and doc.private_stats and not is_owner:
            log.info(
                "public_stats_denied", reason="private_stats", short_code=link.alias
            )
            raise NotFoundError("short_code not found")

        if not is_owner and doc.password:
            if not password:
                log.info(
                    "public_stats_denied",
                    reason="password_required",
                    short_code=link.alias,
                )
                raise PasswordRequiredError("this link's stats are password protected")
            if not self._password_matches(doc, is_v2, password):
                log.info(
                    "public_stats_denied",
                    reason="invalid_password",
                    short_code=link.alias,
                )
                raise InvalidPasswordError("incorrect password")

        owner = doc.owner_id if is_v2 else None
        if not owner or owner == ANONYMOUS_OWNER_ID:
            owner = None
        plan = await self._entitlements.resolve_for(owner)
        whitelabel = bool(owner) and plan.has("domain_polish")
        if whitelabel:
            whitelabel = await self._flags.is_enabled_for_owner(
                DOMAIN_POLISH_FLAG, owner
            )
        # Only the owner gets the plan's window; a public visitor's facet
        # stays sized for the anonymous rate budget.
        window_days = (
            plan.limit("analytics_window_days")
            if is_owner
            else plan_defaults(Plan.FREE)["analytics_window_days"]
        )
        window_start, window_end, tz_name, clamped = self._resolve_window(
            start_date, end_date, tz_name, window_days
        )

        status = link.effective_status()
        facts = self._link_facts(link, doc, status, is_owner=is_owner)

        if is_v2:
            stats = await self._query_v2_stats(
                doc, window_start, window_end, tz_name, clamped=clamped
            )
        else:
            stats = self._synthesize_v1_stats(
                doc, link.alias, window_start, window_end, tz_name
            )

        duration_ms = int((time.perf_counter() - started) * 1000)
        log.info(
            "public_stats_query",
            short_code=link.alias,
            generation=link.generation,
            status=status,
            owner_bypass=is_owner,
            start_date=window_start.isoformat(),
            end_date=window_end.isoformat(),
            total_clicks=stats.get("summary", {}).get("total_clicks", 0),
            duration_ms=duration_ms,
        )

        return {
            "generation": link.generation,
            "link": facts,
            "stats": stats,
            "whitelabel": whitelabel,
        }

    # ── Private: gates and derived facts ──────────────────────────────────────

    @staticmethod
    def _password_matches(doc: _PublicDoc, is_v2: bool, password: str) -> bool:
        """v2 stores an argon2 hash; v1/emoji store plaintext."""
        if is_v2:
            return verify_password(password, doc.password)
        return password == doc.password

    @staticmethod
    def _link_facts(
        link: ResolvedPublicLink,
        doc: _PublicDoc,
        status: str,
        *,
        is_owner: bool,
    ) -> dict[str, Any]:
        long_url = doc.long_url if link.is_v2 else doc.url
        return {
            "alias": link.alias,
            "short_url": link.short_url,
            # Destination-only-while-active, like the preview page: an
            # expired, paused, or blocked link's stats page must not out
            # the destination. Owner sessions always get it.
            "long_url": long_url if (status == "active" or is_owner) else None,
            # The redirect serves this to everyone, so the page names it.
            "expired_redirect_url": (
                doc.expired_redirect_url
                if (link.is_v2 and status == "expired")
                else None
            ),
            "created_at": link.created_at(),
            "status": status,
            "max_clicks": doc.max_clicks,
            "block_bots": bool(doc.block_bots),
            "password_protected": bool(doc.password),
        }

    # ── Private: date window ──────────────────────────────────────────────────

    def _resolve_window(
        self,
        start_raw: str | None,
        end_raw: str | None,
        tz_name: str,
        window_days: int,
    ) -> tuple[datetime, datetime, str, bool]:
        """The dashboard's window rules, then the normalized timezone."""
        start, end, clamped = self._stats.resolve_window(
            start_raw, end_raw, window_days
        )
        return start, end, self._stats.normalize_timezone(tz_name), clamped

    # ── Private: v2 stats (StatsService reuse, scoped by url_id) ─────────────

    async def _query_v2_stats(
        self,
        doc: UrlV2Doc,
        start_date: datetime,
        end_date: datetime,
        tz_name: str,
        *,
        clamped: bool = False,
    ) -> dict[str, Any]:
        """Run the standard $facet aggregation scoped by ``meta.url_id``.

        Matching on url_id — never meta.short_code — so a same-alias link
        on a custom domain can never bleed into the public page.
        """
        click_query: dict[str, Any] = {
            "meta.url_id": doc.id,
            "clicked_at": {"$gte": start_date, "$lte": end_date},
        }
        return await self._stats.compute(
            click_query,
            scope=StatsScope.ANON,
            short_code=doc.alias,
            start_date=start_date,
            end_date=end_date,
            group_by=_V2_GROUP_BY,
            metrics=_METRICS,
            tz_name=tz_name,
            clamped=clamped,
        )

    # ── Private: v1/emoji stats (synthesized from the embedded doc) ──────────

    def _synthesize_v1_stats(
        self,
        doc: LegacyUrlDoc,
        alias: str,
        start_date: datetime,
        end_date: datetime,
        tz_name: str,
    ) -> dict[str, Any]:
        """Build the SAME wire shape from v1's embedded counters.

        Dimension maps are lifetime totals (v1 never stored per-click
        events); only the daily counter time series can honour the window.
        """
        metrics: dict[str, list[dict[str, Any]]] = {}

        # counter keys are UTC day strings written by the click handler.
        start_day = start_date.astimezone(timezone.utc).date().isoformat()
        end_day = end_date.astimezone(timezone.utc).date().isoformat()
        metrics["clicks_by_time"] = [
            {"time": day, "clicks": int(count or 0)}
            for day, count in sorted((doc.counter or {}).items())
            if start_day <= day <= end_day
        ]
        metrics["unique_clicks_by_time"] = [
            {"time": day, "unique_clicks": int(count or 0)}
            for day, count in sorted((doc.unique_counter or {}).items())
            if start_day <= day <= end_day
        ]

        dimensions: list[tuple[str, dict[str, Any], Any]] = [
            ("browser", doc.browser, None),
            # v1 stores the field as os_name; the wire key is "os".
            ("os", doc.os_name, None),
            # v1 stores display names (dots→spaces) — convert to ISO
            # alpha-2 so flags render like v2 ("XX" fallback).
            ("country", doc.country, convert_country_name),
            ("referrer", doc.referrer, None),
        ]
        for key, source, transform in dimensions:
            clicks_rows, unique_rows = self._v1_dimension_rows(source, key, transform)
            metrics[f"clicks_by_{key}"] = clicks_rows
            metrics[f"unique_clicks_by_{key}"] = unique_rows

        # v1 tracks named-bot hits on the doc (no unique variant);
        # v2 never emits this dimension.
        metrics["clicks_by_bots"] = [
            {"bots": name, "clicks": int(count or 0)}
            for name, count in sorted(
                (doc.bots or {}).items(), key=lambda item: item[1], reverse=True
            )
        ]

        last_click = None
        if doc.last_click:
            try:
                last_click = datetime.strptime(
                    doc.last_click, _V1_LAST_CLICK_FORMAT
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                last_click = None

        summary: dict[str, Any] = {
            # Lifetime totals — matching the dimension maps.
            "total_clicks": doc.total_clicks,
            "unique_clicks": len(doc.ips or []),
            "first_click": None,  # not stored on v1 docs
            "last_click": self._stats.to_user_tz(last_click, tz_name),
            # The legacy schema can't tell "never measured" from a true
            # 0.0 average (it stores 0.0 for both), so falsy maps to null
            # — same semantic as v2: null = no measurement, never 0.
            "avg_redirection_time": (
                round(doc.average_redirection_time, 2)
                if doc.average_redirection_time
                else None
            ),
        }

        response: dict[str, Any] = {
            "scope": StatsScope.ANON,
            "filters": {},
            "group_by": list(_V1_GROUP_BY),
            "timezone": tz_name,
            "short_code": alias,
            "time_range": {
                "start_date": self._stats.to_user_tz(start_date, tz_name),
                "end_date": self._stats.to_user_tz(end_date, tz_name),
            },
            # The frontend adapter picks bucket size and zero-fills from
            # this — v1 counters are daily, full stop.
            "time_bucket_info": {
                "strategy": "daily",
                "interval_minutes": 1440,
                "display_format": "%Y-%m-%d",
                "mongo_format": "%Y-%m-%d",
                "timezone": tz_name,
            },
            "metrics": metrics,
            "summary": summary,
        }
        return self._stats.add_metadata(response)

    @staticmethod
    def _v1_dimension_rows(
        source: dict[str, Any] | None,
        key: str,
        transform: Any = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Turn a v1 ``{value: {"counts": n, "ips": [...]}}`` map into wire rows.

        Returns (clicks_rows, unique_rows) sorted by clicks descending —
        unique rows ride the same order so the frontend zip keeps the
        backend's ranking. Values that transform to the same output (e.g.
        two spellings hitting the "XX" country fallback) are merged.

        v1 data spans four years of Flask-era schema drift, so entries
        whose count defies both known forms (a bare list was seen live)
        are SKIPPED — one drifted sub-entry must never 500 the whole
        stats page, and inventing a count for an unknown shape would be
        dishonest. Skips are logged once per dimension, not per row.
        """
        clicks: dict[str, int] = {}
        unique_ips: dict[str, set[str]] = {}
        skipped: list[str] = []
        for raw_value, entry in (source or {}).items():
            if isinstance(entry, dict):
                try:
                    count = int(entry.get("counts", 0) or 0)
                except (TypeError, ValueError):
                    skipped.append(raw_value)
                    continue
                ips = entry.get("ips") or []
                if isinstance(ips, (list, tuple, set)):
                    # v1 wrote plain strings; drifted elements (nested
                    # lists, dicts) don't hash — count stands, uniques don't
                    ips = [ip for ip in ips if isinstance(ip, str)]
                else:
                    ips = []  # unknown container — count stands, uniques don't
            else:  # defensively tolerate bare counters in legacy data
                try:
                    count = int(entry or 0)
                except (TypeError, ValueError):
                    skipped.append(raw_value)
                    continue
                ips = []
            value = transform(raw_value) if transform else raw_value
            clicks[value] = clicks.get(value, 0) + count
            unique_ips.setdefault(value, set()).update(ips)

        if skipped:
            # Cardinality is unbounded (one entry per distinct referrer/etc),
            # so cap the sample — skipped_count carries the true total.
            log.info(
                "v1_dimension_entries_skipped",
                dimension=key,
                skipped_count=len(skipped),
                values=skipped[:20],
            )

        ordered = sorted(clicks, key=lambda value: clicks[value], reverse=True)
        clicks_rows = [{key: value, "clicks": clicks[value]} for value in ordered]
        unique_rows = [
            {key: value, "unique_clicks": len(unique_ips[value])} for value in ordered
        ]
        return clicks_rows, unique_rows
