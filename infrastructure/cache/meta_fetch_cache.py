"""Redis cache for /api/v1/metadata results.

Keys: ``meta_fetch:{sha256(url)}``. Successes cache for an hour (a
destination's tags rarely change mid-session); failures negative-cache
briefly so a flapping destination doesn't burn the caller's rate limit
AND our outbound fetches. None-redis tolerant like url_cache.
"""

from __future__ import annotations

import hashlib
import json

import redis.asyncio as aioredis

from infrastructure.logging import get_logger

log = get_logger(__name__)


class MetaFetchCache:
    def __init__(
        self,
        redis_client: aioredis.Redis | None,
        *,
        ttl_seconds: int = 3600,
        negative_ttl_seconds: int = 300,
        prefix: str = "meta_fetch",
    ) -> None:
        self._redis = redis_client
        self._ttl = ttl_seconds
        self._negative_ttl = negative_ttl_seconds
        self._prefix = prefix

    def _key(self, url: str) -> str:
        return f"{self._prefix}:{hashlib.sha256(url.encode()).hexdigest()}"

    async def get(self, url: str) -> dict | None:
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(self._key(url))
            return json.loads(raw) if raw else None
        except Exception as exc:
            log.warning("meta_fetch_cache_get_error", error=str(exc))
            return None

    async def set(self, url: str, payload: dict, *, negative: bool = False) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.setex(
                self._key(url),
                self._negative_ttl if negative else self._ttl,
                json.dumps(payload),
            )
        except Exception as exc:
            log.warning("meta_fetch_cache_set_error", error=str(exc))
