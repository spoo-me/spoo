"""Subscription matching — which endpoints care about this event.

Stateless predicates over the single event in hand; anything needing
memory is the alerts engine's job, enforced by this class having no
storage access beyond the endpoints query.

The owner cache exists for one reason: at click rate the common
case is "this owner has no webhooks", and that answer must come from
Redis, not Mongo. Write-through invalidation from WebhookService on
every endpoint mutation; degrades to per-event queries without Redis —
which is exactly the deployment rung where click volume is small.
"""

from __future__ import annotations

import redis.asyncio as aioredis
from bson import ObjectId

from app.infrastructure.logging import get_logger
from app.repositories.webhook_endpoint_repository import WebhookEndpointRepository
from app.schemas.models.webhook import WebhookEndpointDoc
from app.services.events.contract import DomainEvent
from app.services.webhooks.registry import expand

log = get_logger(__name__)

_CACHE_KEY = "wh:sub:{owner_id}"


class OwnerSubscriptionCache:
    """owner_id → active endpoint count ("0" short-circuits matching)."""

    def __init__(self, redis_client: aioredis.Redis | None, ttl_seconds: int) -> None:
        self._redis = redis_client
        self._ttl = ttl_seconds

    async def get(self, owner_id: str) -> int | None:
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(_CACHE_KEY.format(owner_id=owner_id))
            return int(raw) if raw is not None else None
        except Exception:
            return None  # cache trouble must never block matching

    async def set(self, owner_id: str, count: int) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.set(
                _CACHE_KEY.format(owner_id=owner_id), str(count), ex=self._ttl
            )
        except Exception:
            log.warning("webhook_sub_cache_set_failed", owner_id=owner_id)

    async def invalidate(self, owner_id: str) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.delete(_CACHE_KEY.format(owner_id=owner_id))
        except Exception:
            log.warning("webhook_sub_cache_invalidate_failed", owner_id=owner_id)


def event_link_id(event: DomainEvent) -> ObjectId | None:
    """Extract the subject link id for scope evaluation (None for
    non-link events — scope never excludes those)."""
    raw = event.data.get("link_id")
    if raw is None:
        link = event.data.get("link")
        if isinstance(link, dict):
            raw = link.get("link_id")
    if isinstance(raw, str) and ObjectId.is_valid(raw):
        return ObjectId(raw)
    return None


class SubscriptionMatcher:
    def __init__(
        self,
        endpoint_repo: WebhookEndpointRepository,
        cache: OwnerSubscriptionCache,
    ) -> None:
        self._repo = endpoint_repo
        self._cache = cache

    async def match(self, event: DomainEvent) -> list[WebhookEndpointDoc]:
        cached = await self._cache.get(event.owner_id)
        if cached == 0:
            return []

        owner_oid = ObjectId(event.owner_id)
        endpoints = await self._repo.find_active_for_owner(owner_oid)
        if cached is None:
            await self._cache.set(event.owner_id, len(endpoints))
        if not endpoints:
            return []

        link_id = event_link_id(event)
        matched = []
        for ep in endpoints:
            # Patterns stored verbatim, expanded at match time so `link.*`
            # subscribers pick up event types added after they subscribed.
            if event.type not in expand(ep.events):
                continue
            if event.type.startswith("link.") and not ep.scope.matches_link(link_id):
                continue
            matched.append(ep)
        return matched
