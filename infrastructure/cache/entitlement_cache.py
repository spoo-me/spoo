"""Per-owner resolved entitlements in Redis: ``ent:{user_id}``.

The TTL is a safety net only. Every repository write to ``subscriptions``
or ``entitlement_overrides`` deletes the key in the same operation, so a
plan change is visible on the owner's next request. Tolerates a missing
Redis client the same way ``UrlCache`` does: every read misses.
"""

from __future__ import annotations

import redis.asyncio as aioredis
from bson import ObjectId

from infrastructure.logging import get_logger
from services.entitlements.resolver import Resolved

log = get_logger(__name__)


class EntitlementCache:
    def __init__(self, redis_client: aioredis.Redis | None, ttl_seconds: int = 600):
        self._redis = redis_client
        self.ttl_seconds = ttl_seconds

    @staticmethod
    def _key(user_id: ObjectId) -> str:
        return f"ent:{user_id}"

    async def get(self, user_id: ObjectId) -> Resolved | None:
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(self._key(user_id))
        except Exception as e:
            log.warning(
                "entitlement_cache_get_error", user_id=str(user_id), error=str(e)
            )
            return None
        if raw is None:
            return None
        try:
            return Resolved.model_validate_json(raw)
        except Exception as e:
            log.warning(
                "entitlement_cache_decode_error", user_id=str(user_id), error=str(e)
            )
            return None

    async def set(self, user_id: ObjectId, resolved: Resolved) -> None:
        if self._redis is None or resolved.degraded:
            return
        try:
            await self._redis.setex(
                self._key(user_id), self.ttl_seconds, resolved.model_dump_json()
            )
        except Exception as e:
            log.warning(
                "entitlement_cache_set_error", user_id=str(user_id), error=str(e)
            )

    async def invalidate(self, user_id: ObjectId) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.delete(self._key(user_id))
        except Exception as e:
            log.error(
                "entitlement_cache_invalidate_error", user_id=str(user_id), error=str(e)
            )
