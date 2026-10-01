"""Redis-backed read-through cache for feature flag documents.

Stores ``FeatureFlagDoc`` as JSON. Negative cache (sentinel string ``MISS``)
prevents repo hammering for unregistered flag names. Cache invalidation is
purely TTL-based — flags mutate via direct mongosh edits (no app event to
trigger active invalidation), so flips propagate within ``ttl_seconds``.

Mirrors ``infrastructure/cache/url_cache.UrlCache`` shape: tolerates
``redis_client is None`` so the app runs without Redis (self-hosters,
local dev) by always missing through to the repo.
"""

from __future__ import annotations

import redis.asyncio as aioredis

from app.infrastructure.logging import get_logger
from app.schemas.models.feature_flag import FeatureFlagDoc

log = get_logger(__name__)

_NEGATIVE_SENTINEL = "MISS"


class FeatureFlagCache:
    def __init__(
        self,
        redis_client: aioredis.Redis | None,
        ttl_seconds: int = 60,
        negative_ttl_seconds: int = 30,
    ) -> None:
        self._redis = redis_client
        self.ttl_seconds = ttl_seconds
        self.negative_ttl_seconds = negative_ttl_seconds

    def _key(self, name: str) -> str:
        return f"flag:{name}"

    async def get(self, name: str) -> FeatureFlagDoc | NegativeMiss | None:
        """Return cached doc, ``NEGATIVE_MISS`` if cached negative, or ``None`` on real miss.

        Distinguishing "cached miss" from "uncached" lets the service skip
        the repo round-trip for known-absent flags.
        """
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(self._key(name))
        except Exception as e:
            log.warning("feature_flag_cache_get_error", name=name, error=str(e))
            return None
        if raw is None:
            return None
        if raw == _NEGATIVE_SENTINEL or raw == _NEGATIVE_SENTINEL.encode():
            return NEGATIVE_MISS
        # Pydantic model_validate_json parses bytes/str transparently.
        try:
            return FeatureFlagDoc.model_validate_json(raw)
        except Exception as e:
            log.warning("feature_flag_cache_decode_error", name=name, error=str(e))
            return None

    async def set(self, name: str, doc: FeatureFlagDoc) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.setex(
                self._key(name),
                self.ttl_seconds,
                doc.model_dump_json(by_alias=True),
            )
        except Exception as e:
            log.error("feature_flag_cache_set_error", name=name, error=str(e))

    async def set_negative(self, name: str) -> None:
        """Cache a negative result (no doc found) with a shorter TTL."""
        if self._redis is None:
            return
        try:
            await self._redis.setex(
                self._key(name),
                self.negative_ttl_seconds,
                _NEGATIVE_SENTINEL,
            )
        except Exception as e:
            log.error("feature_flag_cache_set_negative_error", name=name, error=str(e))

    async def invalidate(self, name: str) -> None:
        """Drop a flag from cache. Used by admin scripts after manual edits.

        Not called from the request path — flips rely on TTL expiry.
        """
        if self._redis is None:
            return
        try:
            await self._redis.delete(self._key(name))
        except Exception as e:
            log.error("feature_flag_cache_invalidate_error", name=name, error=str(e))


class NegativeMiss:
    """Sentinel type for "cache says this flag does not exist".

    Distinct from ``None`` (real cache miss / no Redis). Service compares
    via ``is NEGATIVE_MISS`` to decide whether to skip the repo lookup.
    """


# Module-level singleton — `is` comparison decides "cached miss".
NEGATIVE_MISS = NegativeMiss()
