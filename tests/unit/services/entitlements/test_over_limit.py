"""Over-limit pausing: newest items pause, oldest keep working, and a grown
limit lifts exactly the pauses this policy made."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from bson import ObjectId

from schemas.enums.webhook import EndpointDisabledReason, WebhookStatus
from schemas.models.api_key import ApiKeyDoc
from schemas.models.custom_domain import CustomDomainDoc
from schemas.models.webhook import WebhookEndpointDoc
from services.entitlements import for_plan
from services.entitlements.over_limit import OverLimitService, split_newest
from services.features.catalog import Plan, plan_defaults

UID = ObjectId()
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _at(days: int) -> datetime:
    return T0 + timedelta(days=days)


class TestSplitNewest:
    def test_keeps_the_oldest_within_the_limit(self):
        a, b, c = ObjectId(), ObjectId(), ObjectId()
        keep, pause = split_newest([(c, _at(3)), (a, _at(1)), (b, _at(2))], 2)
        assert keep == [a, b]
        assert pause == [c]

    def test_unlimited_pauses_nothing(self):
        ids = [(ObjectId(), _at(i)) for i in range(5)]
        keep, pause = split_newest(ids, -1)
        assert len(keep) == 5 and pause == []

    def test_zero_limit_pauses_everything(self):
        ids = [(ObjectId(), _at(i)) for i in range(3)]
        keep, pause = split_newest(ids, 0)
        assert keep == [] and len(pause) == 3

    def test_missing_created_at_sorts_last(self):
        a, b = ObjectId(), ObjectId()
        keep, pause = split_newest([(a, None), (b, _at(1))], 1)
        assert keep == [b] and pause == [a]


def _endpoint(created: datetime, status=WebhookStatus.ACTIVE, reason=None):
    return WebhookEndpointDoc(
        _id=ObjectId(),
        user_id=UID,
        url="https://hooks.example/x",
        status=status,
        disabled_reason=reason,
        created_at=created,
    )


def _domain(created: datetime, paused=False):
    return CustomDomainDoc(
        _id=ObjectId(),
        fqdn=f"d{created.day}.example.com",
        owner_id=UID,
        status="active",
        verification_method="cf_http_dcv",
        created_at=created,
        paused_by_limit=paused,
    )


def _key(created: datetime, paused=False, revoked=False):
    return ApiKeyDoc(
        _id=ObjectId(),
        user_id=UID,
        token_prefix="abcd1234",
        token_hash="x" * 64,
        name="k",
        created_at=created,
        paused_by_limit=paused,
        revoked=revoked,
    )


def _service(endpoints=(), domains=(), keys=()):
    ep = AsyncMock()
    ep.find_by_user = AsyncMock(return_value=list(endpoints))
    dm = AsyncMock()
    dm.list_live_by_owner = AsyncMock(return_value=list(domains))
    ks = AsyncMock()
    ks.list_by_user = AsyncMock(return_value=list(keys))
    tenants = AsyncMock()
    owner_cache = AsyncMock()
    svc = OverLimitService(
        endpoints=ep,
        domains=dm,
        keys=ks,
        tenant_resolver=tenants,
        webhook_owner_cache=owner_cache,
    )
    return svc, ep, dm, ks, tenants, owner_cache


def _resolved(**limits):
    values = {**plan_defaults(Plan.FREE), **limits}
    return for_plan(Plan.FREE).model_copy(update={"values": values})


class TestEndpoints:
    @pytest.mark.asyncio
    async def test_free_limit_pauses_the_newest_and_drops_the_matcher_cache(self):
        old, mid, new = _endpoint(_at(1)), _endpoint(_at(2)), _endpoint(_at(3))
        svc, ep, _, _, _, owner_cache = _service(endpoints=[new, old, mid])

        paused = await svc.apply(UID, _resolved(webhook_endpoints_max=1))

        assert paused["webhook_endpoints_max"] == [str(mid.id), str(new.id)]
        disabled = {c.args[0] for c in ep.disable.await_args_list}
        assert disabled == {mid.id, new.id}
        assert all(
            c.args[1] is EndpointDisabledReason.OVER_LIMIT
            for c in ep.disable.await_args_list
        )
        owner_cache.invalidate.assert_awaited_once_with(str(UID))

    @pytest.mark.asyncio
    async def test_grown_limit_lifts_only_our_pauses(self):
        kept = _endpoint(_at(1))
        ours = _endpoint(
            _at(2), WebhookStatus.DISABLED, EndpointDisabledReason.OVER_LIMIT
        )
        theirs = _endpoint(_at(3), WebhookStatus.DISABLED, EndpointDisabledReason.GONE)
        svc, ep, _, _, _, _ = _service(endpoints=[kept, ours, theirs])

        paused = await svc.apply(UID, _resolved(webhook_endpoints_max=10))

        assert paused == {}
        ep.reactivate_over_limit.assert_awaited_once_with(ours.id)
        ep.disable.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_already_disabled_endpoints_are_not_re_disabled(self):
        gone = _endpoint(_at(2), WebhookStatus.DISABLED, EndpointDisabledReason.GONE)
        svc, ep, _, _, _, owner_cache = _service(endpoints=[_endpoint(_at(1)), gone])

        paused = await svc.apply(UID, _resolved(webhook_endpoints_max=1))

        ep.disable.assert_not_awaited()
        owner_cache.invalidate.assert_not_awaited()
        assert paused == {}

    @pytest.mark.asyncio
    async def test_within_limit_touches_nothing(self):
        svc, ep, _, _, _, owner_cache = _service(endpoints=[_endpoint(_at(1))])
        assert await svc.apply(UID, _resolved(webhook_endpoints_max=1)) == {}
        ep.disable.assert_not_awaited()
        owner_cache.invalidate.assert_not_awaited()


class TestDomains:
    @pytest.mark.asyncio
    async def test_lapsed_owner_pauses_every_domain_and_drops_tenant_cache(self):
        d1, d2 = _domain(_at(1)), _domain(_at(2))
        svc, _, dm, _, tenants, _ = _service(domains=[d1, d2])

        paused = await svc.apply(UID, _resolved(custom_domains_max=0))

        assert paused["custom_domains_max"] == [str(d1.id), str(d2.id)]
        dm.set_paused_by_limit.assert_awaited_once_with([d1.id, d2.id], True)
        assert {c.args[0] for c in tenants.invalidate.await_args_list} == {
            d1.fqdn,
            d2.fqdn,
        }

    @pytest.mark.asyncio
    async def test_owner_without_the_feature_pauses_every_domain(self):
        d1, d2 = _domain(_at(1)), _domain(_at(2))
        svc, _, domains, _, tenants, _ = _service(domains=[d1, d2])
        resolved = _resolved(custom_domains=False, custom_domains_max=5)

        paused = await svc.apply(UID, resolved)

        assert set(paused["custom_domains_max"]) == {str(d1.id), str(d2.id)}
        domains.set_paused_by_limit.assert_awaited_once_with([d1.id, d2.id], True)
        assert tenants.invalidate.await_count == 2

    @pytest.mark.asyncio
    async def test_resubscribe_unpauses(self):
        d1 = _domain(_at(1), paused=True)
        svc, _, dm, _, tenants, _ = _service(domains=[d1])

        paused = await svc.apply(
            UID, _resolved(custom_domains=True, custom_domains_max=5)
        )

        assert paused == {}
        dm.set_paused_by_limit.assert_awaited_once_with([d1.id], False)
        tenants.invalidate.assert_awaited_once_with(d1.fqdn)


class TestKeys:
    @pytest.mark.asyncio
    async def test_revoked_keys_do_not_count(self):
        live = [_key(_at(i)) for i in range(3)]
        revoked = _key(_at(0), revoked=True)
        svc, _, _, ks, _, _ = _service(keys=[revoked, *live])

        paused = await svc.apply(UID, _resolved(api_keys_max=2))

        assert paused["api_keys_max"] == [str(live[2].id)]
        ks.set_paused_by_limit.assert_awaited_once_with([live[2].id], True)


class TestPaused:
    @pytest.mark.asyncio
    async def test_reports_only_this_policys_pauses(self):
        ours = _endpoint(
            _at(1), WebhookStatus.DISABLED, EndpointDisabledReason.OVER_LIMIT
        )
        theirs = _endpoint(_at(2), WebhookStatus.DISABLED, EndpointDisabledReason.GONE)
        d = _domain(_at(1), paused=True)
        k = _key(_at(1), paused=True)
        svc, *_ = _service(
            endpoints=[ours, theirs],
            domains=[d, _domain(_at(2))],
            keys=[k, _key(_at(2))],
        )

        assert await svc.paused(UID) == {
            "webhook_endpoints_max": [str(ours.id)],
            "custom_domains_max": [str(d.id)],
            "api_keys_max": [str(k.id)],
        }

    @pytest.mark.asyncio
    async def test_nothing_paused_is_an_empty_map(self):
        svc, *_ = _service(endpoints=[_endpoint(_at(1))])
        assert await svc.paused(UID) == {}
