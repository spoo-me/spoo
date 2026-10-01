"""Redis cache for onboarding resume pointers.

Keys: ``onboarding:{user_id}``, value ``{"step", "path"}``, 24h TTL
(Dub's ONBOARDING_WINDOW). Deliberately soft: the pointer only says
where to resume the wizard — when the TTL lapses or Redis is down the
state reads as empty and the frontend falls back to localStorage.
Completion is NOT stored here; that's a permanent account fact
(``UserDoc.onboarded_at``). None-redis tolerant like url_cache.
"""

from __future__ import annotations

import json

import redis.asyncio as aioredis

from app.infrastructure.logging import get_logger

log = get_logger(__name__)

ONBOARDING_TTL_SECONDS = 24 * 60 * 60


class OnboardingCache:
    def __init__(
        self,
        redis_client: aioredis.Redis | None,
        *,
        ttl_seconds: int = ONBOARDING_TTL_SECONDS,
    ) -> None:
        self._redis = redis_client
        self._ttl = ttl_seconds

    @staticmethod
    def _key(user_id: str) -> str:
        return f"onboarding:{user_id}"

    async def get(self, user_id: str) -> dict | None:
        """Return ``{"step", "path"}`` or None (unset, expired, or Redis down)."""
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(self._key(user_id))
        except Exception as exc:
            log.warning("onboarding_cache_read_failed", error=str(exc))
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    async def set(self, user_id: str, step: str, path: str | None) -> None:
        """Best-effort write; refreshes the TTL."""
        if self._redis is None:
            return
        try:
            await self._redis.set(
                self._key(user_id),
                json.dumps({"step": step, "path": path}),
                ex=self._ttl,
            )
        except Exception as exc:
            log.warning("onboarding_cache_write_failed", error=str(exc))

    async def delete(self, user_id: str) -> None:
        """Drop the pointer (completion, or nothing left to resume)."""
        if self._redis is None:
            return
        try:
            await self._redis.delete(self._key(user_id))
        except Exception as exc:
            log.warning("onboarding_cache_delete_failed", error=str(exc))
