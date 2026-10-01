"""Hot-URL promotion into the Cloudflare KV edge cache.

A :class:`HotUrlAction` (registered in the click worker when
``EDGE_CACHE_*`` is configured): when the hotness detector fires, look
the URL up fresh, gate it through the eligibility rules, and write a
redirect entry into KV. From then until the entry's TTL, the edge
Worker serves that URL's redirects without touching origin.

The lookup is deliberately just ``UrlCache.get``: a URL hot enough to
fire the detector was resolved by the redirect route seconds ago, and
``resolve()`` populates the cache on every miss — so the entry is
present by construction, and exactly as fresh as the cache-invalidation
discipline that the rest of the system already relies on. A miss (worker
restarted mid-burst, cache evicted) is skipped, not repaired: the next
hot window re-fires.
"""

from __future__ import annotations

import asyncio
import random
import time

from app.infrastructure.cache.url_cache import UrlCache, UrlCacheData
from app.infrastructure.cloudflare_kv import CloudflareKVClient
from app.infrastructure.logging import get_logger
from app.schemas.models.url import UrlStatus
from app.services.click.consumers.hotness import HotUrl
from app.services.edge_cache.contract import (
    EdgeCacheEntry,
    EdgeCacheGeoEntry,
    cache_key,
)
from app.services.edge_cache.render import render_meta_preview

log = get_logger(__name__)


def promotion_skip_reason(
    url: UrlCacheData, hot_domain: str, system_domain: str
) -> str | None:
    """Why this URL must NOT be edge-cached — None means eligible.

    Every rule exists because the edge cannot evaluate it: passwords
    need verification, max-clicks needs precise counting, block_bots
    needs UA parsing, expiry/status need fresh state, tenants need
    routing. The edge serves only decisions origin already made.
    """
    if hot_domain != system_domain:
        return "non_system_domain"  # tenant edge-caching is deferred
    if url.url_status != UrlStatus.ACTIVE:
        return "not_active"
    if url.password_hash:
        return "password_protected"
    if url.max_clicks:
        return "max_clicks"
    if url.block_bots:
        return "block_bots"
    if url.expiration_time:
        return "has_expiration"  # could expire mid-TTL; rare, so skip all
    if url.is_not_yet_live(time.time()):
        return "not_yet_live"  # the edge would serve the redirect early
    if url.ab_variants:
        return "ab_variants"  # the Worker has no entry type for a weighted pick
    # geo_rules is NOT a skip: geo links promote as geo_redirect entries —
    # the Worker resolves request.cf.country against the same rules map
    # origin would, so per-country routing is a decision the edge CAN make.
    # Rule edits ride the same TTL-staleness window as plain URL edits.
    return None


class PromoteToEdgeCacheAction:
    """Best-effort by contract (HotUrlAction): every failure is a log
    line, never an exception — a broken promotion must not affect click
    consumption, and the URL simply isn't edge-served this window."""

    def __init__(
        self,
        url_cache: UrlCache,
        kv: CloudflareKVClient,
        *,
        system_domain: str,
        ttl_seconds: int,
        ttl_jitter_ratio: float,
        rng: random.Random | None = None,
    ) -> None:
        self._url_cache = url_cache
        self._kv = kv
        self._system_domain = system_domain
        self._ttl_seconds = ttl_seconds
        self._ttl_jitter_ratio = ttl_jitter_ratio
        self._rng = rng if rng is not None else random.Random()
        self._inflight: set[asyncio.Task] = set()

    async def on_hot(self, hot: HotUrl) -> None:
        """Fire the promotion detached: a degraded CF API (retries +
        timeouts can stack to ~18s) must not stall hotness consumption.
        Concurrency stays naturally bounded — the detector fires once per
        URL per window. Promotions in flight at worker shutdown are
        dropped, which the best-effort contract already permits."""
        task = asyncio.create_task(self.promote(hot))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def promote(self, hot: HotUrl) -> None:
        url = await self._url_cache.get(hot.short_code, hot.domain)
        if url is None:
            self._log_skip(hot, "url_not_in_cache")
            return

        reason = promotion_skip_reason(url, hot.domain, self._system_domain)
        if reason is not None:
            self._log_skip(hot, reason)
            return

        # og-links stay eligible: the worker serves og_html to preview bots
        # and the redirect (geo-aware or plain) to everyone else from the
        # same entry.
        og_html = render_meta_preview(url) if url.meta_title is not None else None
        entry: EdgeCacheEntry | EdgeCacheGeoEntry
        if url.geo_rules:
            entry = EdgeCacheGeoEntry(
                url=url.long_url, rules=url.geo_rules, og_html=og_html
            )
        else:
            entry = EdgeCacheEntry(url=url.long_url, og_html=og_html)
        ttl = self._jittered_ttl()
        ok = await self._kv.put(
            cache_key(hot.domain, hot.short_code),
            entry.to_kv_json(),
            expiration_ttl=ttl,
        )
        if ok:
            log.info(
                "edge_promotion_succeeded",
                domain=hot.domain,
                short_code=hot.short_code,
                ttl_seconds=ttl,
                hot_count=hot.count,
                entry_type=entry.type,
            )
        else:
            # kv client already logged the specifics; this line carries
            # the business context (which URL missed its window).
            log.warning(
                "edge_promotion_failed",
                domain=hot.domain,
                short_code=hot.short_code,
            )

    def _jittered_ttl(self) -> int:
        spread = self._ttl_jitter_ratio
        factor = 1.0 + self._rng.uniform(-spread, spread)
        return max(60, int(self._ttl_seconds * factor))

    def _log_skip(self, hot: HotUrl, reason: str) -> None:
        log.info(
            "edge_promotion_skipped",
            domain=hot.domain,
            short_code=hot.short_code,
            reason=reason,
        )
