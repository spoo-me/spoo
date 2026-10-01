"""EntitlementService: cache, degrade, selfhost, version, transitions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from bson import ObjectId

from errors import InvalidTransitionError
from schemas.models.entitlement_override import EntitlementOverrideDoc, OverrideKind
from schemas.models.subscription import (
    SubscriptionDoc,
    SubscriptionKind,
    SubscriptionProvider,
    SubscriptionStatus,
)
from services.entitlements import EntitlementService, SubscriptionEvent, for_plan
from services.features.catalog import Plan, plan_defaults

UID = ObjectId()
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)


def _sub(status: SubscriptionStatus, **extra) -> SubscriptionDoc:
    return SubscriptionDoc(
        user_id=UID,
        provider=SubscriptionProvider.PADDLE,
        kind=SubscriptionKind.RECURRING,
        status=status,
        current_period_end=NOW + timedelta(days=10),
        **extra,
    )


def _service(*, sub=None, overrides=(), events=3, cached=None, selfhost=False):
    subs = AsyncMock()
    subs.find_by_user = AsyncMock(return_value=sub)
    subs.write_transition = AsyncMock(
        side_effect=lambda uid, **kw: (
            kw["before"] or _sub(kw["after_status"])
        ).model_copy(update={"status": kw["after_status"], **kw["fields"]})
    )
    ovs = AsyncMock()
    ovs.list_active = AsyncMock(return_value=list(overrides))
    evs = AsyncMock()
    evs.count_for = AsyncMock(return_value=events)
    cache = AsyncMock()
    cache.get = AsyncMock(return_value=cached)
    svc = EntitlementService(subs, ovs, evs, cache, selfhost=selfhost)
    return svc, subs, ovs, evs, cache


class TestResolveFor:
    @pytest.mark.asyncio
    async def test_anonymous_never_touches_stores(self):
        svc, subs, _, _, cache = _service()
        r = await svc.resolve_for(None)
        assert r.plan is Plan.ANONYMOUS
        cache.get.assert_not_awaited()
        subs.find_by_user.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_selfhost_short_circuits(self):
        svc, subs, _, _, cache = _service(
            selfhost=True, sub=_sub(SubscriptionStatus.LAPSED)
        )
        r = await svc.resolve_for(UID)
        assert r.plan is Plan.SELFHOST
        assert r.values == plan_defaults(Plan.SELFHOST)
        cache.get.assert_not_awaited()
        subs.find_by_user.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cache_hit_skips_mongo(self):
        cached = for_plan(Plan.PRO, version=9)
        svc, subs, _, _, cache = _service(cached=cached)
        r = await svc.resolve_for(UID)
        assert r is cached
        subs.find_by_user.assert_not_awaited()
        cache.set.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_subscription_is_free_and_cached(self):
        svc, _, _, _, cache = _service(sub=None, events=0)
        r = await svc.resolve_for(UID)
        assert r.plan is Plan.FREE
        assert r.status is None
        assert r.version == 0
        assert r.values == plan_defaults(Plan.FREE)
        cache.set.assert_awaited_once_with(UID, r)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status",
        [
            SubscriptionStatus.ACTIVE,
            SubscriptionStatus.PAST_DUE,
            SubscriptionStatus.CANCEL_AT_PERIOD_END,
            SubscriptionStatus.GRACE,
        ],
    )
    async def test_paying_statuses_resolve_to_pro(self, status):
        svc, *_ = _service(sub=_sub(status, grace_until=NOW + timedelta(days=14)))
        r = await svc.resolve_for(UID)
        assert r.plan is Plan.PRO
        assert r.status is status
        assert r.values["geo_targeting"] is True

    @pytest.mark.asyncio
    async def test_lapsed_resolves_to_free(self):
        svc, *_ = _service(sub=_sub(SubscriptionStatus.LAPSED))
        r = await svc.resolve_for(UID)
        assert r.plan is Plan.FREE
        assert r.status is SubscriptionStatus.LAPSED
        assert r.values["geo_targeting"] is False

    @pytest.mark.asyncio
    async def test_grace_until_is_the_shown_date(self):
        grace_end = NOW + timedelta(days=14)
        svc, *_ = _service(sub=_sub(SubscriptionStatus.GRACE, grace_until=grace_end))
        r = await svc.resolve_for(UID)
        assert r.until == grace_end

    @pytest.mark.asyncio
    async def test_override_applies_on_top_of_plan(self):
        ov = EntitlementOverrideDoc(
            user_id=UID,
            key="geo_targeting",
            value=True,
            kind=OverrideKind.BETA,
            reason="beta",
            granted_by="ops",
        )
        svc, *_ = _service(sub=None, overrides=[ov])
        r = await svc.resolve_for(UID)
        assert r.plan is Plan.FREE
        assert r.values["geo_targeting"] is True

    @pytest.mark.asyncio
    async def test_version_is_the_audit_event_count(self):
        svc, *_ = _service(events=42)
        assert (await svc.resolve_for(UID)).version == 42


class TestDegradedMode:
    @pytest.mark.asyncio
    async def test_store_failure_falls_back_to_free_and_never_caches(self):
        svc, subs, _, _, cache = _service()
        subs.find_by_user.side_effect = RuntimeError("mongo down")
        r = await svc.resolve_for(UID)
        assert r.degraded is True
        assert r.plan is Plan.FREE
        cache.set.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pro_hint_keeps_pro_defaults_when_stores_are_down(self):
        svc, subs, *_ = _service()
        subs.find_by_user.side_effect = RuntimeError("mongo down")
        r = await svc.resolve_for(UID, plan_hint="pro")
        assert r.degraded is True
        assert r.plan is Plan.PRO

    @pytest.mark.asyncio
    async def test_hint_can_never_produce_selfhost(self):
        svc, subs, *_ = _service()
        subs.find_by_user.side_effect = RuntimeError("mongo down")
        r = await svc.resolve_for(UID, plan_hint="selfhost")
        assert r.plan is Plan.FREE

    @pytest.mark.asyncio
    async def test_cache_miss_falls_through_to_mongo(self):
        svc, _, _, _, cache = _service(sub=_sub(SubscriptionStatus.ACTIVE))
        cache.get = AsyncMock(return_value=None)
        r = await svc.resolve_for(UID)
        assert r.plan is Plan.PRO
        assert r.degraded is False

    @pytest.mark.asyncio
    async def test_write_during_compute_is_returned_but_not_cached(self):
        svc, _, _, evs, cache = _service(sub=_sub(SubscriptionStatus.ACTIVE))
        evs.count_for = AsyncMock(side_effect=[3, 4])
        r = await svc.resolve_for(UID)
        assert r.plan is Plan.PRO
        assert r.version == 3
        cache.set.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_version_for_is_none_when_degraded(self):
        svc, subs, *_ = _service()
        subs.find_by_user.side_effect = RuntimeError("down")
        assert await svc.version_for(UID) is None

    @pytest.mark.asyncio
    async def test_plan_hint_for_never_raises(self):
        svc, _, _, _, cache = _service()
        cache.get.side_effect = RuntimeError("boom")
        assert await svc.plan_hint_for(UID) == "free"


class TestTransition:
    @pytest.mark.asyncio
    async def test_granted_from_nothing_writes_active(self):
        svc, subs, *_ = _service(sub=None)
        doc = await svc.transition(
            UID,
            SubscriptionEvent.GRANTED,
            actor="ops",
            reason="manual",
            provider=SubscriptionProvider.MANUAL,
            kind=SubscriptionKind.PREPAID,
            prepaid_until=NOW + timedelta(days=30),
        )
        assert doc.status is SubscriptionStatus.ACTIVE
        kwargs = subs.write_transition.await_args.kwargs
        assert kwargs["before"] is None
        assert kwargs["after_status"] is SubscriptionStatus.ACTIVE
        assert kwargs["fields"]["provider"] is SubscriptionProvider.MANUAL

    @pytest.mark.asyncio
    async def test_rejected_event_never_reaches_the_repository(self):
        svc, subs, *_ = _service(sub=_sub(SubscriptionStatus.LAPSED))
        with pytest.raises(InvalidTransitionError):
            await svc.transition(
                UID, SubscriptionEvent.PAYMENT_SUCCEEDED, actor="paddle", reason="late"
            )
        subs.write_transition.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_grace_requires_grace_until(self):
        svc, subs, *_ = _service(sub=_sub(SubscriptionStatus.ACTIVE))
        with pytest.raises(ValueError):
            await svc.transition(
                UID, SubscriptionEvent.TERM_ENDED, actor="job", reason="term end"
            )
        subs.write_transition.assert_not_awaited()


class TestUsage:
    @pytest.mark.asyncio
    async def test_usage_for_calls_each_counter(self):
        subs, ovs, evs, cache = AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock()
        counter = AsyncMock(return_value=2)
        svc = EntitlementService(
            subs, ovs, evs, cache, selfhost=False, usage={"custom_domains_max": counter}
        )
        assert await svc.usage_for(UID) == {"custom_domains_max": 2}
        counter.assert_awaited_once_with(UID)


class TestOverLimitHook:
    @pytest.mark.asyncio
    async def test_plan_change_reconciles_over_limit(self):
        svc, _, _, _, cache = _service(sub=_sub(SubscriptionStatus.ACTIVE))
        over_limit = AsyncMock()
        over_limit.apply = AsyncMock(return_value={"custom_domains_max": ["x"]})
        svc._over_limit = over_limit
        cache.get = AsyncMock(return_value=None)

        await svc.transition(UID, SubscriptionEvent.ENDED, actor="ops", reason="refund")

        over_limit.apply.assert_awaited_once()
        assert over_limit.apply.await_args.args[0] == UID

    @pytest.mark.asyncio
    async def test_same_plan_transition_does_not_reconcile(self):
        svc, *_ = _service(sub=_sub(SubscriptionStatus.ACTIVE))
        over_limit = AsyncMock()
        svc._over_limit = over_limit

        await svc.transition(
            UID, SubscriptionEvent.PAYMENT_FAILED, actor="paddle", reason="declined"
        )

        over_limit.apply.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_over_limit_for_is_empty_without_the_service(self):
        svc, *_ = _service()
        assert await svc.over_limit_for(UID) == {}
