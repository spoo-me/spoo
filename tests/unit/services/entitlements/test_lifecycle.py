"""The lifecycle job on a clock: real repositories over an in-memory Mongo,
a fake mailer, a frozen clock the test advances by hand."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from bson import ObjectId

from errors import BillingProviderError
from infrastructure.cache.entitlement_cache import EntitlementCache
from repositories.api_key_repository import ApiKeyRepository
from repositories.custom_domain_repository import CustomDomainRepository
from repositories.entitlement_event_repository import EntitlementEventRepository
from repositories.entitlement_override_repository import (
    EntitlementOverrideRepository,
)
from repositories.feature_flag_repository import FeatureFlagRepository
from repositories.subscription_repository import SubscriptionRepository
from repositories.user_repository import UserRepository
from repositories.webhook_endpoint_repository import WebhookEndpointRepository
from schemas.models.entitlement_override import OverrideKind
from schemas.models.subscription import (
    SubscriptionKind,
    SubscriptionProvider,
    SubscriptionStatus,
)
from services.billing.port import ProviderSubscription
from services.entitlements import EntitlementService, SubscriptionEvent
from services.entitlements.lifecycle import LifecycleService, lifecycle_tasks
from services.entitlements.over_limit import OverLimitService
from services.features.catalog import Plan
from tests.fake_mongo import FakeCollection, FakeRedis

UID = ObjectId("aaaaaaaaaaaaaaaaaaaaaaaa")
# In the future relative to the real clock: the resolver reads real time for
# override expiry while the job runs on the test clock.
T0 = datetime(2027, 1, 1, 12, 0, tzinfo=timezone.utc)


class FakeMailer:
    def __init__(self):
        self.sent: list[tuple[str, str, str, dict]] = []
        self.fail = False

    async def send_html(self, to_email, subject, template_name, context, text_body):
        if self.fail:
            return False
        self.sent.append((to_email, subject, template_name, context))
        return True


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


class World:
    def __init__(self, *, provider=None):
        self.redis = FakeRedis()
        cache = EntitlementCache(self.redis)
        self.events = EntitlementEventRepository(FakeCollection("entitlement_events"))
        self.subs = SubscriptionRepository(
            FakeCollection("subscriptions", unique=("user_id",)), self.events, cache
        )
        self.overrides = EntitlementOverrideRepository(
            FakeCollection("entitlement_overrides"), self.events, cache
        )
        self.endpoints = WebhookEndpointRepository(FakeCollection("webhook-endpoints"))
        self.domains = CustomDomainRepository(FakeCollection("custom_domains"))
        self.keys = ApiKeyRepository(FakeCollection("api-keys"))
        self.tenants = AsyncMock()
        over_limit = OverLimitService(
            endpoints=self.endpoints,
            domains=self.domains,
            keys=self.keys,
            tenant_resolver=self.tenants,
            webhook_owner_cache=AsyncMock(),
        )
        self.entitlements = EntitlementService(
            self.subs,
            self.overrides,
            self.events,
            cache,
            selfhost=False,
            over_limit=over_limit,
        )
        self.users = UserRepository(FakeCollection("users"))
        self.flags = FeatureFlagRepository(FakeCollection("feature_flags"))
        self.mailer = FakeMailer()
        self.clock = Clock(T0)
        self.service = LifecycleService(
            self.entitlements,
            self.subs,
            self.overrides,
            self.events,
            self.users,
            self.domains,
            self.flags,
            self.mailer,
            app_url="https://spoo.me",
            provider=provider,
            clock=self.clock,
        )

    async def seed_user(self, email="pro@example.com"):
        await self.users._col.insert_one(
            {"_id": UID, "email": email, "email_verified": True, "status": "ACTIVE"}
        )

    async def grant(self, **fields):
        base = {
            "provider": SubscriptionProvider.PADDLE,
            "kind": SubscriptionKind.RECURRING,
        }
        base.update(fields)
        return await self.entitlements.transition(
            UID, SubscriptionEvent.GRANTED, actor="test", reason="seed", now=T0, **base
        )

    async def status(self) -> SubscriptionStatus:
        return (await self.subs.find_by_user(UID)).status

    async def plan(self) -> Plan:
        return (await self.entitlements.resolve_for(UID)).plan

    async def add_endpoint(self, created: datetime):
        await self.endpoints._col.insert_one(
            {
                "user_id": UID,
                "url": "https://hooks.example/x",
                "events": ["link.clicked"],
                "status": "active",
                "created_at": created,
            }
        )

    async def add_domain(self, fqdn: str):
        await self.domains._col.insert_one(
            {
                "fqdn": fqdn,
                "owner_id": UID,
                "status": "active",
                "verification_method": "cf_http_dcv",
                "created_at": T0,
            }
        )


class TestClockTable:
    @pytest.mark.asyncio
    async def test_prepaid_walks_active_grace_lapsed_and_pauses_over_limit(self):
        w = World()
        await w.seed_user()
        end = T0 + timedelta(days=10)
        await w.grant(kind=SubscriptionKind.PREPAID, prepaid_until=end)
        for created in (T0, T0 + timedelta(hours=1), T0 + timedelta(hours=2)):
            await w.add_endpoint(created)
        await w.add_domain("links.acme.com")
        assert await w.plan() is Plan.PRO

        # One day before the end: still active, the last reminder goes out.
        w.clock.advance(days=9)
        await w.service.tick()
        assert await w.status() is SubscriptionStatus.ACTIVE
        assert [m[2] for m in w.mailer.sent] == ["plan_reminder.html"]

        # Term end: grace with 14 full days from the tick, one grace email.
        w.clock.advance(days=1, seconds=1)
        counts = await w.service.tick()
        assert counts["grace_started"] == 1
        sub = await w.subs.find_by_user(UID)
        assert sub.status is SubscriptionStatus.GRACE
        assert sub.grace_until == w.clock.now + timedelta(days=14)
        assert await w.plan() is Plan.PRO
        assert [m[2] for m in w.mailer.sent] == [
            "plan_reminder.html",
            "plan_grace_started.html",
        ]
        assert w.mailer.sent[1][3]["domains"] == ["links.acme.com"]

        # Another tick in grace: nothing happens twice.
        await w.service.tick()
        assert len(w.mailer.sent) == 2
        assert (await w.subs.find_by_user(UID)).status is SubscriptionStatus.GRACE

        # Grace end: lapsed, free, endpoints beyond the free limit paused,
        # the domain paused, one lapsed email naming the domain and the date.
        w.clock.advance(days=14)
        counts = await w.service.tick()
        assert counts["lapsed"] == 1
        assert await w.status() is SubscriptionStatus.LAPSED
        assert await w.plan() is Plan.FREE
        paused = await w.entitlements.over_limit_for(UID)
        assert len(paused["webhook_endpoints_max"]) == 2
        assert len(paused["custom_domains_max"]) == 1
        statuses = sorted(e.status.value for e in await w.endpoints.find_by_user(UID))
        assert statuses == ["active", "disabled", "disabled"]
        assert w.mailer.sent[-1][2] == "plan_lapsed.html"
        assert w.mailer.sent[-1][3]["domains"] == ["links.acme.com"]
        assert w.mailer.sent[-1][3]["lapsed_date"] == "January 25, 2027"
        assert (await w.subs.find_by_user(UID)).founding_streak_ok is False

        # Ticking on a lapsed subscription is quiet.
        w.clock.advance(days=30)
        await w.service.tick()
        assert len(w.mailer.sent) == 3

    @pytest.mark.asyncio
    async def test_cancel_at_period_end_goes_to_grace_at_the_period_end(self):
        w = World()
        await w.seed_user()
        end = T0 + timedelta(days=3)
        await w.grant(current_period_end=end)
        await w.entitlements.transition(
            UID, SubscriptionEvent.CANCEL_SCHEDULED, actor="paddle", reason="portal"
        )
        w.clock.advance(days=3, seconds=1)
        await w.service.tick()
        sub = await w.subs.find_by_user(UID)
        assert sub.status is SubscriptionStatus.GRACE
        assert sub.grace_until == w.clock.now + timedelta(days=14)

    @pytest.mark.asyncio
    async def test_active_recurring_never_moves_on_the_clock(self):
        w = World()
        await w.seed_user()
        await w.grant(current_period_end=T0 + timedelta(days=1))
        w.clock.advance(days=40)
        await w.service.tick()
        assert await w.status() is SubscriptionStatus.ACTIVE
        assert w.mailer.sent == []

    @pytest.mark.asyncio
    async def test_past_due_ceiling_lapses_seven_days_after_the_period_end(self):
        w = World()
        await w.seed_user()
        end = T0 + timedelta(days=2)
        await w.grant(current_period_end=end)
        await w.entitlements.transition(
            UID, SubscriptionEvent.PAYMENT_FAILED, actor="paddle", reason="declined"
        )
        w.clock.advance(days=8)
        await w.service.tick()
        assert await w.status() is SubscriptionStatus.PAST_DUE
        w.clock.advance(days=1, seconds=1)
        counts = await w.service.tick()
        assert counts["past_due_lapsed"] == 1
        assert await w.status() is SubscriptionStatus.LAPSED
        assert w.mailer.sent[-1][2] == "plan_lapsed.html"


class TestMailsFollowState:
    @pytest.mark.asyncio
    async def test_grace_started_by_a_webhook_still_gets_the_email(self):
        w = World()
        await w.seed_user()
        await w.grant(current_period_end=T0 + timedelta(days=3))
        await w.entitlements.transition(
            UID,
            SubscriptionEvent.TERM_ENDED,
            actor="paddle",
            reason="canceled",
            grace_until=T0 + timedelta(days=17),
        )
        counts = await w.service.tick()
        assert counts["grace_mails"] == 1
        assert [m[2] for m in w.mailer.sent] == ["plan_grace_started.html"]
        await w.service.tick()
        assert len(w.mailer.sent) == 1

    @pytest.mark.asyncio
    async def test_lapsed_mail_survives_a_failed_send(self):
        w = World()
        await w.seed_user()
        await w.grant(
            kind=SubscriptionKind.PREPAID, prepaid_until=T0 + timedelta(days=1)
        )
        w.clock.advance(days=1, seconds=1)
        await w.service.tick()
        w.clock.advance(days=14)
        w.mailer.fail = True
        assert (await w.service.tick())["lapsed"] == 1
        w.mailer.fail = False
        assert (await w.service.tick())["lapsed_mails"] == 1
        assert w.mailer.sent[-1][2] == "plan_lapsed.html"
        assert (await w.service.tick())["lapsed_mails"] == 0

    @pytest.mark.asyncio
    async def test_a_refund_lapse_gets_no_renewal_mail(self):
        w = World()
        await w.seed_user()
        await w.grant(current_period_end=T0 + timedelta(days=30))
        await w.entitlements.transition(
            UID, SubscriptionEvent.ENDED, actor="paddle", reason="full refund"
        )
        assert (await w.subs.find_by_user(UID)).lapsed_by == "ended"
        counts = await w.service.tick()
        assert counts["lapsed_mails"] == 0
        assert w.mailer.sent == []

    @pytest.mark.asyncio
    async def test_a_stale_term_end_still_gets_a_full_grace(self):
        w = World()
        await w.seed_user()
        await w.grant(
            kind=SubscriptionKind.PREPAID, prepaid_until=T0 - timedelta(days=40)
        )
        await w.service.tick()
        sub = await w.subs.find_by_user(UID)
        assert sub.status is SubscriptionStatus.GRACE
        assert sub.grace_until == T0 + timedelta(days=14)

    @pytest.mark.asyncio
    async def test_one_bad_subscription_does_not_stop_the_tick(self):
        w = World()
        await w.seed_user()
        await w.grant(
            kind=SubscriptionKind.PREPAID, prepaid_until=T0 + timedelta(days=1)
        )
        other = ObjectId()
        await w.subs._col.insert_one(
            {
                "user_id": other,
                "provider": "paddle",
                "kind": "prepaid",
                "status": "active",
                "prepaid_until": T0,
                "created_at": T0,
                "updated_at": T0,
            }
        )
        w.clock.advance(days=1, seconds=1)
        w.entitlements.transition = _failing_for(other, w.entitlements.transition)
        counts = await w.service.tick()
        assert counts["grace_started"] == 1
        assert counts["failed"] == 1
        assert await w.status() is SubscriptionStatus.GRACE


def _failing_for(user_id, real):
    async def _transition(uid, *args, **kwargs):
        if uid == user_id:
            raise RuntimeError("bad document")
        return await real(uid, *args, **kwargs)

    return _transition


class TestReminders:
    @pytest.mark.asyncio
    async def test_prepaid_reminders_at_30_7_and_1_days_once_each(self):
        w = World()
        await w.seed_user()
        end = T0 + timedelta(days=60)
        await w.grant(kind=SubscriptionKind.PREPAID, prepaid_until=end)

        w.clock.advance(days=29)
        await w.service.tick()
        assert w.mailer.sent == []

        w.clock.advance(days=1, hours=1)
        assert (await w.service.tick())["reminders"] == 1
        await w.service.tick()
        assert len(w.mailer.sent) == 1
        assert w.mailer.sent[0][3]["days_left"] == 29

        w.clock.advance(days=23)
        assert (await w.service.tick())["reminders"] == 1
        w.clock.advance(days=6)
        assert (await w.service.tick())["reminders"] == 1
        assert [m[3]["days_left"] for m in w.mailer.sent] == [29, 6, 1]
        assert all(m[2] == "plan_reminder.html" for m in w.mailer.sent)

    @pytest.mark.asyncio
    async def test_scheduled_cancel_points_at_the_portal_not_a_new_checkout(self):
        w = World()
        await w.seed_user()
        await w.grant(current_period_end=T0 + timedelta(days=5))
        await w.entitlements.transition(
            UID, SubscriptionEvent.CANCEL_SCHEDULED, actor="paddle", reason="portal"
        )
        assert (await w.service.tick())["reminders"] == 1
        _, _, template, context = w.mailer.sent[0]
        assert template == "plan_reminder.html"
        assert context["scheduled_cancel"] is True
        assert context["grace_days"] == 14

    @pytest.mark.asyncio
    async def test_failed_send_is_retried_next_tick(self):
        w = World()
        await w.seed_user()
        await w.grant(
            kind=SubscriptionKind.PREPAID, prepaid_until=T0 + timedelta(days=5)
        )
        w.mailer.fail = True
        assert (await w.service.tick())["reminders"] == 0
        w.mailer.fail = False
        assert (await w.service.tick())["reminders"] == 1

    @pytest.mark.asyncio
    async def test_reminder_needs_a_user_with_an_email(self):
        w = World()
        await w.grant(
            kind=SubscriptionKind.PREPAID, prepaid_until=T0 + timedelta(days=5)
        )
        assert (await w.service.tick())["reminders"] == 0


class TestOverrides:
    @pytest.mark.asyncio
    async def test_expired_overrides_are_revoked_with_an_event(self):
        w = World()
        await w.overrides.grant(
            UID,
            "geo_targeting",
            True,
            kind=OverrideKind.BETA,
            reason="beta",
            granted_by="ops",
            expires_at=T0 + timedelta(days=1),
        )
        assert (await w.entitlements.resolve_for(UID)).has("geo_targeting")
        version = (await w.entitlements.resolve_for(UID)).version
        await w.service.tick()
        assert (await w.entitlements.resolve_for(UID)).has("geo_targeting")
        w.clock.advance(days=1, seconds=1)
        counts = await w.service.tick()
        assert counts["overrides_pruned"] == 1
        resolved = await w.entitlements.resolve_for(UID)
        assert resolved.has("geo_targeting") is False
        assert resolved.version == version + 1
        kinds = [d["kind"] for d in w.events._col.docs]
        assert kinds[-1] == "override_revoked"

    @pytest.mark.asyncio
    async def test_extended_override_survives_the_prune(self):
        w = World()
        grant = dict(kind=OverrideKind.COMP, reason="comp", granted_by="ops")
        await w.overrides.grant(
            UID, "geo_targeting", True, expires_at=T0 + timedelta(days=1), **grant
        )
        w.clock.advance(days=1, seconds=1)
        # Ops extends between the job's find and its revoke.
        await w.overrides.grant(
            UID, "geo_targeting", True, expires_at=T0 + timedelta(days=30), **grant
        )
        counts = await w.service.tick()
        assert counts["overrides_pruned"] == 0
        assert (await w.entitlements.resolve_for(UID)).has("geo_targeting")

    @pytest.mark.asyncio
    async def test_override_written_elsewhere_is_reconciled_on_the_next_tick(self):
        w = World()
        await w.seed_user()
        await w.grant(current_period_end=T0 + timedelta(days=30))
        for created in (T0, T0 + timedelta(hours=1)):
            await w.add_endpoint(created)
        # An abuse throttle written straight to the store, as the ops tool does.
        await w.overrides.grant(
            UID,
            "webhook_endpoints_max",
            0,
            kind=OverrideKind.ABUSE,
            reason="spam",
            granted_by="ops",
            now=T0,
        )
        counts = await w.service.tick()
        assert counts["overrides_reconciled"] == 1
        paused = await w.entitlements.over_limit_for(UID)
        assert len(paused["webhook_endpoints_max"]) == 2


class TestDailyTasks:
    @pytest.mark.asyncio
    async def test_reconcile_logs_drift_and_never_lapses_on_a_failed_lookup(self):
        provider = AsyncMock()
        provider.name = "paddle"
        w = World(provider=provider)
        await w.grant(provider_ids={"subscription_id": "sub_1", "customer_id": "c"})
        provider.fetch_subscription = AsyncMock(
            return_value=ProviderSubscription(
                id="sub_1", status="canceled", customer_id="c", price_id="p"
            )
        )
        counts = await w.service.reconcile()
        assert counts == {"checked": 1, "drift": 1, "failed": 0}
        assert await w.status() is SubscriptionStatus.ACTIVE

        provider.fetch_subscription = AsyncMock(side_effect=RuntimeError("paddle down"))
        counts = await w.service.reconcile()
        assert counts == {"checked": 1, "drift": 0, "failed": 1}
        assert await w.status() is SubscriptionStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_pending_cancel_phase_is_quiet_on_the_null_provider(self):
        provider = AsyncMock()
        provider.name = "none"
        w = World(provider=provider)
        await w.grant(
            kind=SubscriptionKind.PREPAID,
            prepaid_until=T0 + timedelta(days=365),
            provider_ids={"subscription_id": "sub_1", "cancel_pending": "sub_1"},
        )
        counts = await w.service.tick()
        assert counts["cancels_scheduled"] == 0
        assert counts["failed"] == 0
        provider.fetch_subscription.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tick_retries_a_pending_cancel_until_the_provider_takes_it(self):
        provider = AsyncMock()
        provider.name = "paddle"
        w = World(provider=provider)
        await w.grant(
            kind=SubscriptionKind.PREPAID,
            prepaid_until=T0 + timedelta(days=365),
            provider_ids={"subscription_id": "sub_1", "cancel_pending": "sub_1"},
        )
        live = ProviderSubscription(
            id="sub_1", status="active", customer_id="c", price_id="p"
        )
        provider.fetch_subscription = AsyncMock(return_value=live)
        provider.schedule_cancel = AsyncMock(
            side_effect=BillingProviderError("paddle down")
        )
        assert (await w.service.tick())["cancels_scheduled"] == 0
        assert (await w.subs.find_by_user(UID)).provider_ids[
            "cancel_pending"
        ] == "sub_1"
        provider.schedule_cancel = AsyncMock()
        assert (await w.service.tick())["cancels_scheduled"] == 1
        provider.schedule_cancel.assert_awaited_once_with("sub_1")
        assert "cancel_pending" not in (await w.subs.find_by_user(UID)).provider_ids

    @pytest.mark.asyncio
    async def test_reconcile_is_skipped_without_a_provider(self):
        w = World()
        assert await w.service.reconcile() == {"skipped": True}

    @pytest.mark.asyncio
    async def test_pro_value_check_ignores_free_features_and_treats_empty_rollouts_as_off(
        self,
    ):
        w = World()
        await w.flags._col.insert_one(
            {"name": "webhooks", "enabled": False, "rollout_type": "everyone"}
        )
        await w.flags._col.insert_one(
            {"name": "geo_targeting", "enabled": True, "rollout_type": "allowlist"}
        )
        await w.flags._col.insert_one(
            {
                "name": "custom_meta_tags",
                "enabled": True,
                "rollout_type": "percentage",
                "percentage": 0,
            }
        )
        await w.flags._col.insert_one(
            {
                "name": "ab_testing",
                "enabled": True,
                "rollout_type": "hex_digit",
                "enabled_digits": [],
            }
        )
        withheld = (await w.service.pro_value_check())["withheld"]
        assert "webhooks" not in withheld
        assert "geo_targeting" in withheld
        assert "custom_meta_tags" in withheld
        assert "ab_variants" in withheld

    @pytest.mark.asyncio
    async def test_pro_value_check_lists_pro_features_whose_flag_is_off(self):
        w = World()
        await w.flags._col.insert_one(
            {"name": "geo_targeting", "enabled": True, "rollout_type": "everyone"}
        )
        await w.flags._col.insert_one(
            {"name": "custom_meta_tags", "enabled": False, "rollout_type": "everyone"}
        )
        await w.flags._col.insert_one(
            {"name": "ab_testing", "enabled": True, "rollout_type": "off"}
        )
        result = await w.service.pro_value_check()
        withheld = result["withheld"]
        assert "geo_targeting" not in withheld
        assert "custom_meta_tags" in withheld
        assert "ab_variants" in withheld
        assert "expired_fallback" in withheld


def test_task_names_and_cadence():
    tasks = lifecycle_tasks(AsyncMock())
    assert [(t.name, t.schedule) for t in tasks] == [
        ("entitlements-lifecycle", "* * * * *"),
        ("billing-reconcile", "0 4 * * *"),
        ("entitlements-pro-value-check", "30 4 * * *"),
    ]


@pytest.mark.parametrize(
    ("template", "context", "expected"),
    [
        (
            "plan_grace_started.html",
            {"grace_date": "January 25, 2027", "grace_days": 14},
            "until January 25, 2027",
        ),
        (
            "plan_lapsed.html",
            {"lapsed_date": "January 25, 2027"},
            "ended on January 25, 2027",
        ),
        (
            "plan_reminder.html",
            {
                "end_date": "January 25, 2027",
                "days_left": 7,
                "grace_days": 14,
                "scheduled_cancel": True,
            },
            "/dashboard/settings",
        ),
    ],
)
def test_templates_render_with_the_context_the_job_builds(template, context, expected):
    from unittest.mock import MagicMock

    from config import EmailSettings
    from infrastructure.email.zeptomail import ZeptoMailProvider

    provider = ZeptoMailProvider(
        EmailSettings(), MagicMock(), app_url="https://spoo.me"
    )
    html = provider._jinja.get_template(template).render(
        app_url="https://spoo.me", domains=["links.acme.com"], **context
    )
    assert expected in html
    assert "links.acme.com" in html
    assert "14" in html
