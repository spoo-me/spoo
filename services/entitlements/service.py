"""
Entitlement resolution for a principal, with the per-owner versioned cache.

``resolve_for`` is the one entry point for both read paths: the request
path (user id from the token or API key) and the redirect path (owner id
from the URL document). It never raises: when the stores are unreachable
it returns the cached map, else the caller's plan hint, else free defaults,
marked ``degraded`` so nothing writes it back to the cache.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from bson import ObjectId

from infrastructure.cache.entitlement_cache import EntitlementCache
from infrastructure.logging import get_logger
from repositories.entitlement_event_repository import EntitlementEventRepository
from repositories.entitlement_override_repository import (
    EntitlementOverrideRepository,
)
from repositories.subscription_repository import SubscriptionRepository
from schemas.models.subscription import SubscriptionDoc, SubscriptionStatus
from services.entitlements.over_limit import OverLimitService
from services.entitlements.resolver import ANONYMOUS, Resolved, for_plan, resolve
from services.entitlements.state_machine import SubscriptionEvent, next_status
from services.features.catalog import Plan

log = get_logger(__name__)

UsageCounter = Callable[[ObjectId], Awaitable[int]]


class EntitlementService:
    def __init__(
        self,
        subscriptions: SubscriptionRepository,
        overrides: EntitlementOverrideRepository,
        events: EntitlementEventRepository,
        cache: EntitlementCache,
        *,
        selfhost: bool,
        usage: dict[str, UsageCounter] | None = None,
        over_limit: OverLimitService | None = None,
    ) -> None:
        self._subs = subscriptions
        self._overrides = overrides
        self._events = events
        self._cache = cache
        self._selfhost = selfhost
        self._usage = usage or {}
        self._over_limit = over_limit

    @property
    def selfhost(self) -> bool:
        return self._selfhost

    # ── Read ─────────────────────────────────────────────────────────────

    async def resolve_for(
        self, user_id: ObjectId | None, *, plan_hint: str | None = None
    ) -> Resolved:
        if user_id is None:
            return ANONYMOUS
        if self._selfhost:
            return for_plan(Plan.SELFHOST)

        cached = await self._cache.get(user_id)
        if cached is not None:
            return cached

        try:
            resolved, settled = await self._compute(user_id)
        except Exception as e:
            log.error("entitlements_degraded", user_id=str(user_id), error=str(e))
            return self._fallback(plan_hint)

        if settled:
            await self._cache.set(user_id, resolved)
        return resolved

    async def _compute(self, user_id: ObjectId) -> tuple[Resolved, bool]:
        """The resolved map, and whether it is safe to cache.

        A write landing between the two version reads would leave the cache
        holding the old map under the new version for a full TTL, so that
        answer is returned but not stored.
        """
        now = datetime.now(timezone.utc)
        version = await self._events.count_for(user_id)
        sub = await self._subs.find_by_user(user_id)
        overrides = await self._overrides.list_active(user_id, now)
        settled = await self._events.count_for(user_id) == version
        plan = sub.effective_plan if sub else Plan.FREE
        resolved = Resolved(
            plan=plan,
            status=sub.status if sub else None,
            until=sub.until if sub else None,
            founding=bool(sub and sub.founding),
            values=resolve(plan, overrides),
            version=version,
        )
        return resolved, settled

    @staticmethod
    def _fallback(plan_hint: str | None) -> Resolved:
        # A hint can only ever downgrade the answer: pro is the ceiling and
        # anything unrecognised is free.
        plan = Plan.PRO if plan_hint == Plan.PRO.value else Plan.FREE
        fallback = for_plan(plan)
        return fallback.model_copy(update={"degraded": True})

    async def plan_hint_for(self, user_id: ObjectId) -> str:
        """Plan name for the JWT claim. Never raises."""
        try:
            return (await self.resolve_for(user_id)).plan.value
        except Exception:
            return Plan.FREE.value

    async def version_for(self, user_id: ObjectId) -> int | None:
        resolved = await self.resolve_for(user_id)
        return None if resolved.degraded else resolved.version

    async def usage_for(self, user_id: ObjectId) -> dict[str, int]:
        """Live ``used`` counts for the limits that count something."""
        out: dict[str, int] = {}
        for key, counter in self._usage.items():
            out[key] = await counter(user_id)
        return out

    async def over_limit_for(self, user_id: ObjectId) -> dict[str, list[str]]:
        if self._over_limit is None:
            return {}
        return await self._over_limit.paused(user_id)

    async def reconcile_over_limit(self, user_id: ObjectId) -> dict[str, list[str]]:
        """Pause or unpause the owner's items to fit what they hold now."""
        if self._over_limit is None:
            return {}
        return await self._over_limit.apply(user_id, await self.resolve_for(user_id))

    # ── Write ────────────────────────────────────────────────────────────

    async def transition(
        self,
        user_id: ObjectId,
        event: SubscriptionEvent,
        *,
        actor: str,
        reason: str,
        now: datetime | None = None,
        **fields: Any,
    ) -> SubscriptionDoc:
        """Apply ``event`` to the user's subscription.

        The state machine decides the next status; the repository writes it
        with the audit event and cache invalidation. Raises
        ``InvalidTransitionError`` for an event the current status rejects.
        """
        before = await self._subs.find_by_user(user_id)
        after_status = next_status(before.status if before else None, event)
        if after_status is SubscriptionStatus.GRACE and "grace_until" not in fields:
            raise ValueError("a transition to grace needs grace_until")
        if after_status is SubscriptionStatus.LAPSED:
            fields["founding_streak_ok"] = False
        after = await self._subs.write_transition(
            user_id,
            before=before,
            after_status=after_status,
            fields=fields,
            actor=actor,
            reason=reason,
            now=now,
        )
        before_plan = before.effective_plan if before else Plan.FREE
        if after.effective_plan != before_plan:
            # The transition is already durable; a failed pause is retried by
            # the lifecycle tick, not by failing the caller.
            try:
                await self.reconcile_over_limit(user_id)
            except Exception as e:
                log.error(
                    "over_limit_reconcile_failed", user_id=str(user_id), error=str(e)
                )
        return after
