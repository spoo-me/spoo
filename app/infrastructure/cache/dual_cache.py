"""Async dual-cache (primary + stale + lock pattern).

Behaviour mirrors the synchronous cache/dual_cache.py:
  1. Return live data if primary key exists.
  2. Return stale data if primary expired; fire-and-forget background refresh.
  3. Fetch from DB if both keys are absent; populate both keys.
  4. Return None on lock contention (caller should respond with 204).

The background refresh thread becomes asyncio.create_task() — fire-and-forget,
errors are logged and swallowed so the request is never affected.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import redis.asyncio as aioredis

from app.infrastructure.logging import get_logger

log = get_logger(__name__)


class DualCache:
    def __init__(
        self,
        redis_client: aioredis.Redis | None,
        primary_ttl: int = 300,
        stale_ttl: int = 900,
        lock_ttl: int = 30,
    ) -> None:
        self._redis = redis_client
        self.primary_ttl = primary_ttl
        self.stale_ttl = stale_ttl
        self.lock_ttl = lock_ttl
        self._background_tasks: set[asyncio.Task] = set()

    async def _lock(self, key: str) -> bool:
        """Acquire a Redis SET NX EX lock. Returns True if acquired."""
        if self._redis is None:
            return False
        result = await self._redis.set(key, "1", nx=True, ex=self.lock_ttl)
        return result is not None

    async def get_or_set(
        self,
        base_key: str,
        query_fn: Callable[[], Awaitable[Any]],
        serializer_fn: Callable[[Any], Any] | None = None,
        primary_ttl: int | None = None,
        stale_ttl: int | None = None,
    ) -> Any:
        """Return cached data, or call query_fn and populate the cache.

        If Redis is unavailable, always calls query_fn directly.
        """
        if self._redis is None:
            return await query_fn()

        effective_primary_ttl = (
            primary_ttl if primary_ttl is not None else self.primary_ttl
        )
        effective_stale_ttl = stale_ttl if stale_ttl is not None else self.stale_ttl

        primary_key = f"{base_key}:live"
        stale_key = f"{base_key}:stale"
        lock_key = f"{base_key}:lock"

        def _serialize(data: Any) -> str:
            return json.dumps(serializer_fn(data) if serializer_fn else data)

        # 1. Primary hit
        raw = await self._redis.get(primary_key)
        if raw:
            return json.loads(raw)

        # 2. Stale hit — return stale, trigger background refresh
        stale = await self._redis.get(stale_key)
        if stale:
            if await self._lock(lock_key):
                task = asyncio.create_task(
                    self._refresh(
                        base_key,
                        query_fn,
                        serializer_fn,
                        effective_primary_ttl,
                        effective_stale_ttl,
                    )
                )
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            return json.loads(stale)

        # 3. Full miss — fetch from DB
        if await self._lock(lock_key):
            try:
                data = await query_fn()
                serialized = _serialize(data)
                await self._redis.setex(primary_key, effective_primary_ttl, serialized)
                await self._redis.setex(stale_key, effective_stale_ttl, serialized)
                await self._redis.delete(lock_key)
                return data
            except Exception as e:
                log.error(
                    "dual_cache_query_failed",
                    base_key=base_key,
                    error=str(e),
                    error_type=type(e).__name__,
                )
                # Lock intentionally not released — expires after lock_ttl to
                # rate-limit retries while the upstream query is broken.
                return None

        # 4. Lock contention
        log.debug("dual_cache_lock_contention", base_key=base_key)
        return None

    async def _refresh(
        self,
        base_key: str,
        query_fn: Callable[[], Awaitable[Any]],
        serializer_fn: Callable[[Any], Any] | None = None,
        primary_ttl: int | None = None,
        stale_ttl: int | None = None,
    ) -> None:
        """Background refresh — errors are logged and swallowed."""
        effective_primary_ttl = (
            primary_ttl if primary_ttl is not None else self.primary_ttl
        )
        effective_stale_ttl = stale_ttl if stale_ttl is not None else self.stale_ttl
        try:
            data = await query_fn()
            serialized = json.dumps(serializer_fn(data) if serializer_fn else data)
            await self._redis.setex(
                f"{base_key}:live", effective_primary_ttl, serialized
            )
            await self._redis.setex(
                f"{base_key}:stale", effective_stale_ttl, serialized
            )
        except Exception as e:
            log.error(
                "cache_refresh_failed",
                base_key=base_key,
                error=str(e),
                error_type=type(e).__name__,
            )
