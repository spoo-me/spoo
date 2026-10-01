"""
Over-limit pausing: when a plan's count limit shrinks below what an owner
already has, the newest items pause and the oldest keep working.

Every countable limit with a ``pause_newest`` policy is reconciled the same
way: sort the owner's items oldest first, keep the first ``max``, pause the
rest, and unpause anything this policy paused earlier that now fits. The
pause is stored on the item itself (so the redirect path, the webhook matcher
and API key auth each read one flag) and surfaced as ``over_limit`` in
``/me/entitlements``. Nothing is deleted.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from bson import ObjectId

from infrastructure.logging import get_logger
from repositories.api_key_repository import ApiKeyRepository
from repositories.custom_domain_repository import CustomDomainRepository
from repositories.webhook_endpoint_repository import WebhookEndpointRepository
from schemas.enums.webhook import EndpointDisabledReason, WebhookStatus
from services.entitlements.resolver import Resolved
from services.features.catalog import UNLIMITED
from services.tenant_resolver.protocol import TenantResolver

log = get_logger(__name__)


class _OwnerCache(Protocol):
    async def invalidate(self, owner_id: str) -> None: ...


def split_newest(
    items: list[tuple[ObjectId, datetime | None]], limit: int
) -> tuple[list[ObjectId], list[ObjectId]]:
    """(keep, pause): oldest ``limit`` items keep working, the rest pause."""
    ordered = sorted(items, key=lambda it: (it[1] is None, it[1] or 0, str(it[0])))
    ids = [oid for oid, _ in ordered]
    if limit == UNLIMITED or limit >= len(ids):
        return ids, []
    return ids[:limit], ids[limit:]


class OverLimitService:
    def __init__(
        self,
        *,
        endpoints: WebhookEndpointRepository,
        domains: CustomDomainRepository,
        keys: ApiKeyRepository,
        tenant_resolver: TenantResolver,
        webhook_owner_cache: _OwnerCache,
    ) -> None:
        self._endpoints = endpoints
        self._domains = domains
        self._keys = keys
        self._tenants = tenant_resolver
        self._webhook_owner_cache = webhook_owner_cache

    async def apply(
        self, user_id: ObjectId, resolved: Resolved
    ) -> dict[str, list[str]]:
        """Pause and unpause the owner's items to fit ``resolved``. Returns
        the ids paused per limit key (empty when everything fits)."""
        paused = {
            "webhook_endpoints_max": await self._apply_endpoints(
                user_id, resolved.limit("webhook_endpoints_max")
            ),
            "custom_domains_max": await self._apply_domains(
                user_id,
                resolved.limit("custom_domains_max")
                if resolved.has("custom_domains")
                else 0,
            ),
            "api_keys_max": await self._apply_keys(
                user_id, resolved.limit("api_keys_max")
            ),
        }
        result = {k: [str(i) for i in v] for k, v in paused.items() if v}
        if result:
            log.info("over_limit_applied", user_id=str(user_id), paused=result)
        return result

    async def paused(self, user_id: ObjectId) -> dict[str, list[str]]:
        """Ids currently paused by this policy, per limit key."""
        endpoints = [
            str(e.id)
            for e in await self._endpoints.find_by_user(user_id)
            if e.disabled_reason is EndpointDisabledReason.OVER_LIMIT
        ]
        domains = [
            str(d.id)
            for d in await self._domains.list_live_by_owner(user_id)
            if d.paused_by_limit
        ]
        keys = [
            str(k.id)
            for k in await self._keys.list_by_user(user_id)
            if k.paused_by_limit
        ]
        out = {
            "webhook_endpoints_max": endpoints,
            "custom_domains_max": domains,
            "api_keys_max": keys,
        }
        return {k: v for k, v in out.items() if v}

    async def _apply_endpoints(self, user_id: ObjectId, limit: int) -> list[ObjectId]:
        docs = await self._endpoints.find_by_user(user_id)
        keep, pause = split_newest([(d.id, d.created_at) for d in docs], limit)
        by_id = {d.id: d for d in docs}
        changed = False
        for oid in pause:
            doc = by_id[oid]
            if doc.status is not WebhookStatus.DISABLED:
                await self._endpoints.disable(oid, EndpointDisabledReason.OVER_LIMIT)
                changed = True
        for oid in keep:
            if by_id[oid].disabled_reason is EndpointDisabledReason.OVER_LIMIT:
                await self._endpoints.reactivate_over_limit(oid)
                changed = True
        if changed:
            await self._webhook_owner_cache.invalidate(str(user_id))
        return [
            oid
            for oid in pause
            if by_id[oid].status is not WebhookStatus.DISABLED
            or by_id[oid].disabled_reason is EndpointDisabledReason.OVER_LIMIT
        ]

    async def _apply_domains(self, user_id: ObjectId, limit: int) -> list[ObjectId]:
        docs = await self._domains.list_live_by_owner(user_id)
        keep, pause = split_newest([(d.id, d.created_at) for d in docs], limit)
        by_id = {d.id: d for d in docs}
        to_pause = [oid for oid in pause if not by_id[oid].paused_by_limit]
        to_unpause = [oid for oid in keep if by_id[oid].paused_by_limit]
        if to_pause:
            await self._domains.set_paused_by_limit(to_pause, True)
        if to_unpause:
            await self._domains.set_paused_by_limit(to_unpause, False)
        for oid in to_pause + to_unpause:
            await self._tenants.invalidate(by_id[oid].fqdn)
        return pause

    async def _apply_keys(self, user_id: ObjectId, limit: int) -> list[ObjectId]:
        docs = [k for k in await self._keys.list_by_user(user_id) if not k.revoked]
        keep, pause = split_newest([(d.id, d.created_at) for d in docs], limit)
        by_id = {d.id: d for d in docs}
        to_pause = [oid for oid in pause if not by_id[oid].paused_by_limit]
        to_unpause = [oid for oid in keep if by_id[oid].paused_by_limit]
        if to_pause:
            await self._keys.set_paused_by_limit(to_pause, True)
        if to_unpause:
            await self._keys.set_paused_by_limit(to_unpause, False)
        return pause
