"""Redis-backed Mongo TenantResolver.

Read-through cache with TTL. Negative results (unknown hosts) cached for a
shorter TTL so newly-registered domains become visible quickly. The
system-default host short-circuits before touching Redis or Mongo.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import redis.asyncio as aioredis
from bson import ObjectId

from app.infrastructure.logging import get_logger
from app.repositories.custom_domain_repository import CustomDomainRepository
from app.schemas.enums.domain_status import DomainStatus
from app.schemas.models.base import ANONYMOUS_OWNER_ID
from app.services.tenant_resolver.protocol import TenantInfo, TenantResolver
from app.shared.url_utils import is_system_default_host

log = get_logger(__name__)

# Stored as the Redis VALUE under negative-cache keys so a follow-up read
# can distinguish "we already checked and it's unknown" from "no key set".
_NEG_SENTINEL = "__none__"

# Distinct sentinel OBJECTS returned by _cache_get so callers can branch
# between (a) cache miss → must hit Mongo, (b) negative-cached → skip
# Mongo and return None.
_CACHE_MISS = object()
_NEG_CACHE_HIT = object()

# Sentinel value of a tombstone key. Any non-empty value works — readers
# only check existence; the value is informational.
_TOMB_VALUE = "1"


class CachedMongoTenantResolver(TenantResolver):
    def __init__(
        self,
        repo: CustomDomainRepository,
        redis_client: aioredis.Redis | None,
        system_default_domain: str,
        positive_ttl_seconds: int = 60,
        # Negative TTL can be aggressive because the orchestrator calls
        # ``invalidate(fqdn)`` on every state transition
        negative_ttl_seconds: int = 300,
        # Tombstone window: how long after invalidate() new cache writes
        # for the same host are skipped. Defends against the read-through
        # race where a slow resolve() reads stale Mongo data, then races
        # invalidate() and writes the stale answer back.
        tombstone_ttl_seconds: int = 5,
    ) -> None:
        self._repo = repo
        self._redis = redis_client
        self._system_default_domain = system_default_domain.lower().rstrip(".")
        self._positive_ttl = positive_ttl_seconds
        self._negative_ttl = negative_ttl_seconds
        self._tombstone_ttl = tombstone_ttl_seconds

    def _key(self, host: str) -> str:
        return f"tenant:{host}"

    def _tomb_key(self, host: str) -> str:
        return f"tenant_tomb:{host}"

    @staticmethod
    def _normalise_host(host: str) -> str:
        """Lowercased, dot-stripped, port-stripped host.

        Defers to ``urllib.parse.urlsplit`` so bracketed IPv6 literals
        (``[::1]:8000`` → ``::1``) work per RFC 3986 — splitting on ``:``
        directly would lose the address.
        """
        if not host:
            return ""
        try:
            parsed_host = urlsplit(f"//{host.strip()}").hostname
        except ValueError:
            # Malformed input (unbalanced brackets, etc.) — treat as unknown.
            return ""
        if not parsed_host:
            return ""
        return parsed_host.rstrip(".")

    async def resolve(self, host: str) -> TenantInfo | None:
        normalised = self._normalise_host(host)
        if not normalised:
            return None

        # System default + its `www.` alias short-circuit. No Redis, no
        # Mongo, no cache slot consumed. Shared predicate keeps this rule in
        # lockstep with the read surfaces that fold onto the default domain.
        if is_system_default_host(normalised, self._system_default_domain):
            return TenantInfo(
                domain_id=None,
                fqdn=self._system_default_domain,
                owner_id=None,
                status=DomainStatus.ACTIVE,
                is_system_default=True,
            )

        cached = await self._cache_get(normalised)
        if cached is _NEG_CACHE_HIT:
            # We already asked Mongo about this host and it didn't exist —
            # short-circuit so scanner traffic / typos don't repeat the query.
            return None
        if cached is not _CACHE_MISS:
            # Positive cache hit — `cached` is the TenantInfo.
            return cached

        doc = await self._repo.find_active_by_fqdn(normalised)
        if doc is None:
            await self._cache_set_negative(normalised)
            return None

        info = TenantInfo(
            domain_id=doc.id,
            fqdn=doc.fqdn,
            owner_id=doc.owner_id
            if doc.owner_id and doc.owner_id != ANONYMOUS_OWNER_ID
            else None,
            status=doc.status,
            is_system_default=bool(doc.is_system_default),
            root_redirect=doc.root_redirect,
            not_found_redirect=doc.not_found_redirect,
            custom_robots_txt=doc.custom_robots_txt,
        )
        await self._cache_set(normalised, info)
        return info

    async def invalidate(self, host: str) -> None:
        if self._redis is None:
            return
        normalised = self._normalise_host(host)
        if not normalised or normalised == self._system_default_domain:
            # System default short-circuits resolve(); no cache slot to drop.
            return
        try:
            # Atomic delete + tombstone via pipeline. Tombstone blocks any
            # in-flight resolve() from writing stale data back to the cache
            # for the next ``tombstone_ttl_seconds`` — see _cache_set /
            # _cache_set_negative for the read-side check.
            pipe = self._redis.pipeline()
            pipe.delete(self._key(normalised))
            pipe.setex(self._tomb_key(normalised), self._tombstone_ttl, _TOMB_VALUE)
            await pipe.execute()
        except Exception as exc:
            log.warning("tenant_cache_invalidate_error", host=host, error=str(exc))

    async def _is_tombstoned(self, host: str) -> bool:
        """Return True if invalidate() recently fired for *host*.

        Used as the anti-stale guard before any cache write. Failures here
        degrade safely to ``False`` — the cache write proceeds (no worse
        than current behaviour).
        """
        if self._redis is None:
            return False
        try:
            return await self._redis.get(self._tomb_key(host)) is not None
        except Exception as exc:
            log.warning("tenant_tomb_check_error", host=host, error=str(exc))
            return False

    # ── Cache helpers ────────────────────────────────────────────────

    async def _cache_get(self, host: str):
        """Return ``_CACHE_MISS``, ``_NEG_CACHE_HIT``, or a ``TenantInfo``.

        Three-state return so resolve() can distinguish "I haven't asked
        yet" from "I already asked and there's nothing" — the latter must
        skip Mongo to be useful as a negative cache.
        """
        if self._redis is None:
            return _CACHE_MISS
        try:
            raw = await self._redis.get(self._key(host))
        except Exception as exc:
            log.warning("tenant_cache_get_error", host=host, error=str(exc))
            return _CACHE_MISS
        if raw is None:
            return _CACHE_MISS
        # Redis client may return bytes or str depending on decode_responses.
        # Normalise to str once so downstream comparisons work either way.
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if raw == _NEG_SENTINEL:
            return _NEG_CACHE_HIT
        try:
            payload = json.loads(raw)
            return TenantInfo(
                domain_id=ObjectId(payload["domain_id"])
                if payload.get("domain_id")
                else None,
                fqdn=payload["fqdn"],
                owner_id=ObjectId(payload["owner_id"])
                if payload.get("owner_id")
                else None,
                status=DomainStatus(payload["status"]),
                is_system_default=payload.get("is_system_default", False),
                root_redirect=payload.get("root_redirect"),
                not_found_redirect=payload.get("not_found_redirect"),
                custom_robots_txt=payload.get("custom_robots_txt"),
            )
        except Exception as exc:
            # Corrupt cache entry — log and treat as a miss so the caller
            # falls through to Mongo and overwrites with fresh data.
            log.warning("tenant_cache_decode_error", host=host, error=str(exc))
            return _CACHE_MISS

    async def _cache_set(self, host: str, info: TenantInfo) -> None:
        if self._redis is None:
            return
        # Anti-stale guard: invalidate() may have fired while we were
        # querying Mongo — if so, skip the write so we don't poison the
        # cache with the now-stale answer.
        if await self._is_tombstoned(host):
            return
        try:
            payload = json.dumps(
                {
                    "domain_id": str(info.domain_id) if info.domain_id else None,
                    "fqdn": info.fqdn,
                    "owner_id": str(info.owner_id) if info.owner_id else None,
                    "status": info.status.value,
                    "is_system_default": info.is_system_default,
                    "root_redirect": info.root_redirect,
                    "not_found_redirect": info.not_found_redirect,
                    "custom_robots_txt": info.custom_robots_txt,
                }
            )
            await self._redis.setex(self._key(host), self._positive_ttl, payload)
        except Exception as exc:
            log.warning("tenant_cache_set_error", host=host, error=str(exc))

    async def _cache_set_negative(self, host: str) -> None:
        if self._redis is None:
            return
        if await self._is_tombstoned(host):
            return
        try:
            await self._redis.setex(self._key(host), self._negative_ttl, _NEG_SENTINEL)
        except Exception as exc:
            log.warning("tenant_cache_set_negative_error", host=host, error=str(exc))
