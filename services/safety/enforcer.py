"""SafetyEnforcer — turns a toxic verdict into reality, everywhere at once.

Order matters: collect the invalidation set first (the flip removes docs
from the not-yet-blocked filters), then flip, then evict Redis + edge so
the next click rebuilds from Mongo and serves the 451. Owned links emit
link.blocked domain events (anonymous links have no possible webhook
subscriber). v1/emoji links have no status machine — they carry a single
``blocked`` flag, flipped here with the same blocked_at/blocked_reason
audit stamp the v2 docs get, and reversible where the manual deletes this
replaces never were.

Idempotent by construction: re-enforcing an already-blocked host matches
nothing and is free, which is what lets repeat reports re-run enforcement
instead of reasoning about state.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass

from infrastructure.cache.url_cache import UrlCache
from infrastructure.cloudflare_kv import CloudflareKVClient
from infrastructure.logging import get_logger
from repositories.legacy.emoji_url_repository import EmojiUrlRepository
from repositories.legacy.legacy_url_repository import LegacyUrlRepository
from repositories.url_repository import UrlRepository
from schemas.models.url import UrlStatus, UrlV2Doc
from services.edge_cache.contract import cache_key
from services.events.contract import DomainEvent
from services.events.protocol import DomainEventSink
from services.webhooks.payloads import link_owner_id, link_snapshot
from shared.alias_dispatch import v2_lookup_code

log = get_logger(__name__)


@dataclass(frozen=True)
class EnforcementResult:
    host: str
    blocked_count: int
    legacy_count: int
    cache_invalidated: int


@dataclass(frozen=True)
class AliasEnforcementResult:
    host: str
    blocked_count: int
    cache_invalidated: int


class SafetyEnforcer:
    def __init__(
        self,
        url_repo: UrlRepository,
        legacy_repo: LegacyUrlRepository,
        emoji_repo: EmojiUrlRepository,
        url_cache: UrlCache,
        *,
        events: DomainEventSink | None = None,
        edge_kv: CloudflareKVClient | None = None,
        system_default_domain: str = "spoo.me",
    ) -> None:
        self._url_repo = url_repo
        self._legacy_repo = legacy_repo
        self._emoji_repo = emoji_repo
        self._url_cache = url_cache
        self._events = events
        self._edge_kv = edge_kv
        self._system_domain = system_default_domain

    async def block_host(self, host: str, *, reason: str) -> EnforcementResult:
        # 1. Collect BEFORE the flip, status-blind: a re-delivered block must
        #    still evict entries the first attempt flipped but failed to evict.
        triples = await self._url_repo.list_by_dest_host_with_urls(host)
        pairs = [(alias, domain) for alias, domain, _ in triples]
        owned = await self._url_repo.list_active_owned_by_dest_host(host)
        legacy_ids = [c for c, _ in await self._legacy_repo.list_by_dest_host(host)]
        emoji_ids = [a for a, _ in await self._emoji_repo.list_by_dest_host(host)]

        # 2. Flip — all three collections.
        blocked = await self._url_repo.block_active_by_dest_host(host, reason=reason)
        legacy = await self._legacy_repo.block_by_dest_host(host, reason=reason)
        legacy += await self._emoji_repo.block_by_dest_host(host, reason=reason)

        # 3+4. Evict Redis + edge KV. v1/emoji only ever live on the
        # system domain; emoji cache keys use the canonical VS16 form,
        # not the stored ``_id`` variant.
        evicted = await self._evict(
            pairs,
            system_extra=[
                *legacy_ids,
                *(v2_lookup_code(alias) for alias in emoji_ids),
            ],
        )

        # 5. link.blocked for owned links; sink never raises. v1/emoji are
        #    anonymous by construction — no possible webhook subscriber.
        await self._emit_blocked(owned, reason)

        log.info(
            "safety_host_blocked",
            host=host,
            blocked_count=blocked,
            legacy_count=legacy,
            cache_invalidated=evicted,
            reason=reason,
        )
        return EnforcementResult(
            host=host,
            blocked_count=blocked,
            legacy_count=legacy,
            cache_invalidated=evicted,
        )

    async def block_matching(
        self, host: str, *, matcher, reason: str
    ) -> EnforcementResult:
        """Block every link to *host* whose long URL satisfies *matcher*,
        across all three collections; a host verdict is deliberately NOT
        implied."""
        triples = await self._url_repo.list_by_dest_host_with_urls(host)
        legacy_rows = await self._legacy_repo.list_by_dest_host(host)
        emoji_rows = await self._emoji_repo.list_by_dest_host(host)

        # Up to 150k regex evaluations, off the loop this process shares
        # with clicks, webhooks and the scheduler.
        def _select() -> tuple[list, list, list]:
            return (
                [(a, d) for a, d, url in triples if matcher(url)],
                [(c, url) for c, url in legacy_rows if matcher(url)],
                [(a, url) for a, url in emoji_rows if matcher(url)],
            )

        pairs, legacy_hits, emoji_hits = await asyncio.to_thread(_select)

        owned = await self._url_repo.list_active_owned_by_aliases(pairs)
        blocked = await self._url_repo.block_active_by_aliases(pairs, reason=reason)
        legacy = await self._legacy_repo.block_by_ids(
            [code for code, _ in legacy_hits], reason=reason
        )
        legacy += await self._emoji_repo.block_by_ids(
            [alias for alias, _ in emoji_hits], reason=reason
        )

        evicted = await self._evict(
            pairs,
            system_extra=[
                *(code for code, _ in legacy_hits),
                *(v2_lookup_code(alias) for alias, _ in emoji_hits),
            ],
        )
        await self._emit_blocked(owned, reason)

        log.info(
            "safety_matching_blocked",
            host=host,
            blocked_count=blocked,
            legacy_count=legacy,
            cache_invalidated=evicted,
            reason=reason,
        )
        return EnforcementResult(
            host=host,
            blocked_count=blocked,
            legacy_count=legacy,
            cache_invalidated=evicted,
        )

    async def unblock_host(self, host: str) -> EnforcementResult:
        """Reverse a wrong host-wide block: flip everything carrying the
        safety stamp, then evict so a rebuilt entry sees the restored state.
        Manual operator bans (no ``blocked_reason``) are never touched."""
        unblocked = await self._url_repo.unblock_by_dest_host(host)
        legacy_ids = [c for c, _ in await self._legacy_repo.list_by_dest_host(host)]
        emoji_ids = [a for a, _ in await self._emoji_repo.list_by_dest_host(host)]
        legacy = await self._legacy_repo.unblock_by_dest_host(host)
        legacy += await self._emoji_repo.unblock_by_dest_host(host)
        triples = await self._url_repo.list_by_dest_host_with_urls(host)
        evicted = await self._evict(
            [(alias, domain) for alias, domain, _ in triples],
            system_extra=[
                *legacy_ids,
                *(v2_lookup_code(alias) for alias in emoji_ids),
            ],
        )
        log.info(
            "safety_host_unblocked",
            host=host,
            unblocked_count=unblocked,
            legacy_count=legacy,
            cache_invalidated=evicted,
        )
        return EnforcementResult(
            host=host,
            blocked_count=unblocked,
            legacy_count=legacy,
            cache_invalidated=evicted,
        )

    async def block_aliases(
        self, pairs: list[tuple[str, str]], *, host: str, reason: str
    ) -> AliasEnforcementResult:
        """Per-link enforcement: block specific v2 (alias, domain) links
        without a host-wide verdict — a compromised legitimate site keeps
        serving, only the abusive links die. The doc-level blocked_reason
        is the only reason-of-record here (no verdict doc exists). Same
        collect → flip → evict → notify order, idempotent the same way."""
        owned = await self._url_repo.list_active_owned_by_aliases(pairs)
        blocked = await self._url_repo.block_active_by_aliases(pairs, reason=reason)
        evicted = await self._evict(pairs)
        await self._emit_blocked(owned, reason)
        log.info(
            "safety_aliases_blocked",
            host=host,
            blocked_count=blocked,
            cache_invalidated=evicted,
            reason=reason,
        )
        return AliasEnforcementResult(
            host=host, blocked_count=blocked, cache_invalidated=evicted
        )

    async def _evict(
        self,
        pairs: list[tuple[str, str]],
        *,
        system_extra: list[str] | None = None,
    ) -> int:
        """Redis per domain namespace, then edge KV (system domain only —
        tenant domains never promote). Best-effort: the KV client returns
        bool, never raises. Returns the number of keys evicted."""
        by_domain: dict[str, list[str]] = defaultdict(list)
        for alias, domain in pairs:
            by_domain[domain or self._system_domain].append(alias)
        if system_extra:
            by_domain[self._system_domain].extend(system_extra)
        for domain, aliases in by_domain.items():
            if aliases:
                await self._url_cache.invalidate_many(aliases, domain)
        if self._edge_kv is not None:
            system_aliases = by_domain.get(self._system_domain, [])
            if system_aliases:
                await self._edge_kv.bulk_delete(
                    [cache_key(self._system_domain, a) for a in system_aliases]
                )
        return sum(len(v) for v in by_domain.values())

    async def _emit_blocked(self, owned: list[UrlV2Doc], reason: str) -> None:
        if self._events is None:
            return
        for doc in owned:
            owner = link_owner_id(doc)
            if owner is None:
                continue
            snapshot_doc = doc.model_copy(update={"status": UrlStatus.BLOCKED})
            await self._events.emit(
                DomainEvent(
                    type="link.blocked",
                    owner_id=owner,
                    data={"link": link_snapshot(snapshot_doc), "reason": reason},
                )
            )
