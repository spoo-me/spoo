"""Eager KV write-through for custom meta-tags (edge-first serving).

On every meta-relevant write, origin renders the OG page once and puts an
``og_only`` entry in Workers KV so the edge answers preview bots with zero
origin round-trips. Humans always pass through (the worker only serves
``og_only`` entries to preview crawlers), so click tracking is unaffected.

Lifecycle: put on set/edit, delete on clear/deactivate/delete. The put
carries a TTL (refreshed on every edit) as a backstop — it bounds how
long a stale entry can linger if a delete fails or a status change skips
update() (e.g. an admin sets BLOCKED directly in Mongo).

Best-effort like promotion: a KV outage must never fail a user's API
write — worst case bots reach origin, which answers correctly.

Scope: system-domain v2 links only. Custom-domain tenants never route
through the worker (CF-for-SaaS), so KV entries for them would be dead
weight; origin serves their previews directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.infrastructure.logging import get_logger
from app.schemas.models.url import UrlStatus
from app.services.edge_cache.contract import EdgeCacheEntry, cache_key
from app.services.edge_cache.render import render_meta_preview

if TYPE_CHECKING:
    from app.infrastructure.cache.url_cache import UrlCacheData
    from app.infrastructure.cloudflare_kv import CloudflareKVClient

log = get_logger(__name__)


def build_og_entry(url: UrlCacheData) -> tuple[str, str]:
    """``(key, value)`` for an ACTIVE og-link's KV entry.

    The single source of the ``og_only`` entry shape — used by ``sync``
    below and by the bulk ops' edge flush, so the two paths can never
    drift on what an og entry looks like.
    """
    entry = EdgeCacheEntry(type="og_only", og_html=render_meta_preview(url))
    return cache_key(url.domain, url.alias), entry.to_kv_json()


class OgEdgeWritethrough:
    def __init__(
        self, kv: CloudflareKVClient, *, system_domain: str, ttl_seconds: int = 86_400
    ) -> None:
        self._kv = kv
        self._system_domain = system_domain
        self._ttl_seconds = ttl_seconds

    async def sync(self, url: UrlCacheData) -> None:
        """Reconcile the KV og entry with the link's current state.

        Callers invoke this only for links that have meta_tags now or had
        them before the write — never for plain links, whose promoted
        redirect entries must not be touched.
        """
        if url.domain != self._system_domain:
            return
        key = cache_key(url.domain, url.alias)
        try:
            if url.meta_title is not None and url.url_status == UrlStatus.ACTIVE:
                _, value = build_og_entry(url)
                ok = await self._kv.put(key, value, expiration_ttl=self._ttl_seconds)
                log.info(
                    "og_writethrough_put",
                    short_code=url.alias,
                    domain=url.domain,
                    ok=ok,
                )
            else:
                # Cleared / inactive / blocked: drop the whole key. If the
                # link was also hot-promoted this wipes its redirect entry
                # too — it re-promotes within one hotness window.
                ok = await self._kv.delete(key)
                log.info(
                    "og_writethrough_delete",
                    short_code=url.alias,
                    domain=url.domain,
                    ok=ok,
                )
        except Exception:
            log.exception(
                "og_writethrough_failed", short_code=url.alias, domain=url.domain
            )

    async def remove(self, domain: str, alias: str) -> None:
        """Drop the entry for a deleted/renamed/moved og-link (old key)."""
        if domain != self._system_domain:
            return
        try:
            await self._kv.delete(cache_key(domain, alias))
            log.info("og_writethrough_delete", short_code=alias, domain=domain, ok=True)
        except Exception:
            log.exception("og_writethrough_failed", short_code=alias, domain=domain)
