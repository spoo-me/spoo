"""Billing through the routes: checkout, portal, and every handled webhook,
with real services over an in-memory Mongo and a recorded Paddle transport.

The only fakes are the storage and the network. The webhook route, the
signature check, the dedupe ledger, the state machine, the repository
writes and the resolver are all the real objects.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

from config import BillingSettings
from dependencies import (
    get_billing_service,
    get_current_user,
    get_entitlement_service,
    get_feature_flag_service,
    require_jwt,
    require_jwt_verified,
)
from infrastructure.cache.entitlement_cache import EntitlementCache
from repositories.billing_event_repository import BillingEventRepository
from repositories.entitlement_event_repository import EntitlementEventRepository
from repositories.entitlement_override_repository import (
    EntitlementOverrideRepository,
)
from repositories.subscription_repository import SubscriptionRepository
from services.billing import BillingService, PaddleProvider
from services.entitlements import EntitlementService, SubscriptionEvent
from services.feature_flag_service import FeatureFlagService
from tests.fake_mongo import FakeCollection, FakeRedis

from .conftest import _build_test_app, _make_user

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "paddle"
USER_ID = ObjectId("aaaaaaaaaaaaaaaaaaaaaaaa")
SECRET = "pdl_ntfset_test_secret"
SUB_ID = "sub_01hv8x29kz0t586xy6zn1a62ny"
WEBHOOK = "/api/v1/billing/webhooks/paddle"
_PADDLE_ENV = {
    "BILLING_PROVIDER": "paddle",
    "BILLING_PADDLE_API_KEY": "pdl_sdbx_test",
    "BILLING_PADDLE_WEBHOOK_SECRET": SECRET,
    "BILLING_PADDLE_PRICE_PRO_MONTHLY": "pri_pro_monthly_test",
    "BILLING_PADDLE_PRICE_PRO_YEAR": "pri_pro_year_test",
}


def _load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _sign(raw: bytes) -> str:
    ts = int(time.time())
    h1 = hmac.new(SECRET.encode(), f"{ts}:".encode() + raw, hashlib.sha256).hexdigest()
    return f"ts={ts};h1={h1}"


class Paddle:
    """The provider's view of the world: what GET returns for each entity,
    and what was POSTed."""

    def __init__(self):
        self.subscriptions: dict[str, dict] = {}
        self.transactions: dict[str, dict] = {}
        self.adjustments: dict[str, dict] = {}
        self.posts: list[tuple[str, dict]] = []
        self.fail_cancel = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST":
            body = json.loads(request.content or b"{}")
            if path.endswith("/cancel") and self.fail_cancel:
                return httpx.Response(500, json={"error": "try later"})
            self.posts.append((path, body))
            if path == "/transactions":
                return httpx.Response(
                    201,
                    json={
                        "data": {
                            "id": "txn_new",
                            "checkout": {
                                "url": "https://spoo.me/upgrade/checkout?_ptxn=txn_new"
                            },
                        }
                    },
                )
            if path.endswith("/portal-sessions"):
                return httpx.Response(
                    201,
                    json={
                        "data": {
                            "urls": {"general": {"overview": "https://portal.test/x"}}
                        }
                    },
                )
            return httpx.Response(200, json={"data": {"id": path.split("/")[2]}})
        for prefix, store in (
            ("/subscriptions/", self.subscriptions),
            ("/transactions/", self.transactions),
            ("/adjustments/", self.adjustments),
        ):
            if path.startswith(prefix):
                entity = store.get(path[len(prefix) :])
                if entity is None:
                    return httpx.Response(404, json={"error": "not found"})
                return httpx.Response(200, json={"data": entity})
        return httpx.Response(404)

    def set_from_fixture(self, name: str) -> dict:
        payload = _load(name)
        data = payload["data"]
        if payload["event_type"].startswith("subscription."):
            self.subscriptions[data["id"]] = data
        elif payload["event_type"].startswith("transaction."):
            self.transactions[data["id"]] = data
        elif payload["event_type"].startswith("adjustment."):
            self.adjustments[data["id"]] = data
        return payload


class World:
    def __init__(self, *, founding_opened: bool = True, redis: FakeRedis | None = None):
        self.redis = redis if redis is not None else FakeRedis()
        cache = EntitlementCache(self.redis)
        self.events = EntitlementEventRepository(FakeCollection("entitlement_events"))
        self.subs = SubscriptionRepository(
            FakeCollection("subscriptions", unique=("user_id",)), self.events, cache
        )
        overrides = EntitlementOverrideRepository(
            FakeCollection("entitlement_overrides"), self.events, cache
        )
        self.entitlements = EntitlementService(
            self.subs, overrides, self.events, cache, selfhost=False
        )
        self.paddle = Paddle()
        self.billing_events = BillingEventRepository(
            FakeCollection("billing_events", unique=("event_id",))
        )
        self.settings = BillingSettings(
            provider="paddle",
            paddle_api_key="pdl_sdbx_test",
            paddle_webhook_secret=SECRET,
            paddle_price_pro_monthly="pri_pro_monthly_test",
            paddle_price_pro_year="pri_pro_year_test",
            paddle_discount_founding_first_monthly="dsc_founding_first_test",
            paddle_discount_founding_first_year="dsc_founding_first_year_test",
            paddle_discount_founding_renew_monthly="dsc_founding_renew_test",
            paddle_discount_founding_renew_year="dsc_founding_renew_year_test",
            founding_opened_at=datetime.now(timezone.utc) - timedelta(days=1)
            if founding_opened
            else None,
        )
        provider = PaddleProvider(
            httpx.AsyncClient(transport=httpx.MockTransport(self.paddle.handler)),
            api_key="pdl_sdbx_test",
            webhook_secret=SECRET,
            env="sandbox",
        )
        self.billing = BillingService(
            provider,
            self.entitlements,
            self.subs,
            self.billing_events,
            self.redis,
            self.settings,
            app_url="https://spoo.me",
        )
        self.user = _make_user(user_id=USER_ID, email_verified=True)
        flags = FeatureFlagService.__new__(FeatureFlagService)

        async def _on(name, user):
            return True

        flags.is_enabled = _on  # type: ignore[method-assign]
        # The app's own settings must say paddle too, or /plans has nothing to sell.
        with patch.dict(os.environ, _PADDLE_ENV):
            self.app = _build_test_app(
                {
                    get_billing_service: lambda: self.billing,
                    get_entitlement_service: lambda: self.entitlements,
                    get_feature_flag_service: lambda: flags,
                    get_current_user: lambda: self.user,
                    require_jwt: lambda: self.user,
                    require_jwt_verified: lambda: self.user,
                }
            )
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def deliver(self, name: str, *, signature: str | None = "valid") -> httpx.Response:
        payload = self.paddle.set_from_fixture(name)
        raw = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if signature == "valid":
            headers["Paddle-Signature"] = _sign(raw)
        elif signature is not None:
            headers["Paddle-Signature"] = signature
        return self.client.post(WEBHOOK, content=raw, headers=headers)

    def me(self) -> dict[str, Any]:
        return self.client.get("/api/v1/me/entitlements").json()

    async def sub(self):
        return await self.subs.find_by_user(USER_ID)


# ── Checkout and portal ──────────────────────────────────────────────────────


class TestCheckout:
    def test_monthly_checkout_attaches_the_founding_discount_and_our_page(self):
        w = World()
        resp = w.client.post(
            "/api/v1/billing/checkout",
            json={
                "cadence": "monthly",
                "from": "geo_targeting",
                "return": "/dashboard/links",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["url"].endswith("_ptxn=txn_new")
        path, body = w.paddle.posts[-1]
        assert path == "/transactions"
        assert body["items"] == [{"price_id": "pri_pro_monthly_test", "quantity": 1}]
        assert body["discount_id"] == "dsc_founding_first_test"
        assert body["custom_data"] == {
            "user_id": str(USER_ID),
            "cadence": "monthly",
            "return_to": "/dashboard/links",
            "founding": True,
            "from": "geo_targeting",
        }
        assert body["checkout"]["url"] == (
            "https://spoo.me/upgrade/checkout?return=/dashboard/links&from=geo_targeting"
        )

    def test_year_checkout_uses_the_one_time_price(self):
        w = World()
        w.client.post("/api/v1/billing/checkout", json={"cadence": "year"})
        _, body = w.paddle.posts[-1]
        assert body["items"][0]["price_id"] == "pri_pro_year_test"
        assert body["discount_id"] == "dsc_founding_first_year_test"
        assert body["custom_data"]["return_to"] == "/dashboard"

    def test_closed_founding_window_attaches_no_discount(self):
        w = World(founding_opened=False)
        w.client.post("/api/v1/billing/checkout", json={"cadence": "monthly"})
        _, body = w.paddle.posts[-1]
        assert "discount_id" not in body
        assert body["custom_data"]["founding"] is False

    def test_second_monthly_checkout_while_subscribed_is_409(self):
        w = World()
        w.deliver("subscription_activated")
        resp = w.client.post("/api/v1/billing/checkout", json={"cadence": "monthly"})
        assert resp.status_code == 409
        assert (
            w.client.post(
                "/api/v1/billing/checkout", json={"cadence": "year"}
            ).status_code
            == 200
        )

    def test_monthly_checkout_during_a_live_prepaid_term_is_409(self):
        w = World()
        w.deliver("transaction_completed_prepaid")
        resp = w.client.post("/api/v1/billing/checkout", json={"cadence": "monthly"})
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_prepaid_customer_in_grace_can_buy_monthly(self):
        w = World()
        w.deliver("transaction_completed_prepaid")
        await w.entitlements.transition(
            USER_ID,
            SubscriptionEvent.TERM_ENDED,
            actor="test",
            reason="term over",
            grace_until=datetime.now(timezone.utc) + timedelta(days=14),
        )
        assert w.me()["plan"]["status"] == "grace"
        resp = w.client.post("/api/v1/billing/checkout", json={"cadence": "monthly"})
        assert resp.status_code == 200

    def test_return_must_be_a_local_path(self):
        w = World()
        resp = w.client.post(
            "/api/v1/billing/checkout",
            json={"cadence": "monthly", "return": "https://evil.example/"},
        )
        assert resp.status_code == 422
        assert w.paddle.posts == []

    def test_portal_needs_a_billing_account(self):
        w = World()
        assert w.client.post("/api/v1/billing/portal").status_code == 404

    def test_portal_after_activation(self):
        w = World()
        assert w.deliver("subscription_activated").status_code == 200
        resp = w.client.post("/api/v1/billing/portal")
        assert resp.status_code == 200
        assert resp.json()["url"] == "https://portal.test/x"
        path, body = w.paddle.posts[-1]
        assert path == "/customers/ctm_01hv6y1jedq4p1n0yqn5ba3ky4/portal-sessions"
        assert body == {"subscription_ids": [SUB_ID]}

    def test_plans_reports_founding_seats_and_window(self):
        w = World()
        body = w.client.get("/api/v1/plans").json()
        assert body["founding"]["seats_left"] == 100
        assert body["founding"]["until"] is not None
        w.deliver("subscription_activated")
        assert w.client.get("/api/v1/plans").json()["founding"]["seats_left"] == 99


# ── Webhooks ─────────────────────────────────────────────────────────────────


class TestWebhookGates:
    @pytest.mark.asyncio
    async def test_bad_signature_is_401_and_stores_nothing(self):
        w = World()
        resp = w.deliver("subscription_activated", signature="ts=1;h1=deadbeef")
        assert resp.status_code == 401
        assert await w.billing_events.find_by_event_id("evt_activated_0001") is None
        assert await w.sub() is None

    @pytest.mark.asyncio
    async def test_missing_signature_is_401(self):
        w = World()
        assert w.deliver("subscription_activated", signature=None).status_code == 401

    @pytest.mark.asyncio
    async def test_duplicate_delivery_is_a_no_op(self):
        w = World()
        first = w.deliver("subscription_activated")
        assert first.status_code == 200 and first.json()["outcome"] == "applied"
        version = w.me()["version"]
        second = w.deliver("subscription_activated")
        assert second.status_code == 200 and second.json()["outcome"] == "duplicate"
        assert w.me()["version"] == version
        assert await w.events.count_for(USER_ID) == 1

    @pytest.mark.asyncio
    async def test_concurrent_duplicate_gets_409_while_locked(self):
        redis = FakeRedis()
        w = World(redis=redis)
        await redis.set(f"billing:lock:{SUB_ID}", "1", nx=True, ex=30)
        resp = w.deliver("subscription_activated")
        assert resp.status_code == 409
        # Recorded but not applied: the provider's retry must still apply it.
        assert await w.billing_events.find_by_event_id("evt_activated_0001") is not None
        await redis.delete(f"billing:lock:{SUB_ID}")
        assert w.deliver("subscription_activated").json()["outcome"] == "applied"
        assert w.me()["plan"]["name"] == "pro"
        assert w.deliver("subscription_activated").json()["outcome"] == "duplicate"

    @pytest.mark.asyncio
    async def test_failed_delivery_is_retried_not_deduped(self):
        w = World()
        payload = _load("subscription_activated")
        raw = json.dumps(payload).encode()
        headers = {"Paddle-Signature": _sign(raw)}
        # First delivery: the re-fetch 404s and the row is marked failed.
        assert w.client.post(WEBHOOK, content=raw, headers=headers).status_code == 502
        w.paddle.set_from_fixture("subscription_activated")
        resp = w.client.post(WEBHOOK, content=raw, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["outcome"] == "applied"
        assert w.me()["plan"]["name"] == "pro"

    @pytest.mark.asyncio
    async def test_unknown_user_is_recorded_as_ignored_and_answers_200(self):
        w = World()
        resp = w.deliver("subscription_activated_unknown_user")
        assert resp.status_code == 200
        assert resp.json()["outcome"] == "ignored_unknown"
        stored = await w.billing_events.find_by_event_id("evt_unknown_0009")
        assert stored.outcome.value == "ignored_unknown"
        assert await w.sub() is None

    @pytest.mark.asyncio
    async def test_provider_failure_is_500_so_paddle_retries(self):
        w = World()
        payload = _load("subscription_activated")
        # Not registered with the fake Paddle: the re-fetch 404s.
        raw = json.dumps(payload).encode()
        resp = w.client.post(
            WEBHOOK, content=raw, headers={"Paddle-Signature": _sign(raw)}
        )
        assert resp.status_code == 502
        stored = await w.billing_events.find_by_event_id("evt_activated_0001")
        assert stored.outcome.value == "failed"
        assert await w.sub() is None


class TestWebhookFlows:
    @pytest.mark.asyncio
    async def test_activation_grants_pro_from_the_refetched_subscription(self):
        w = World()
        assert w.me()["plan"]["name"] == "free"
        resp = w.deliver("subscription_activated")
        assert resp.json()["outcome"] == "applied"
        me = w.me()
        assert me["plan"]["name"] == "pro"
        assert me["plan"]["status"] == "active"
        assert me["plan"]["founding"] is True
        assert me["features"]["geo_targeting"] == "enabled"
        sub = await w.sub()
        assert sub.provider_ids == {
            "subscription_id": SUB_ID,
            "customer_id": "ctm_01hv6y1jedq4p1n0yqn5ba3ky4",
        }
        assert sub.kind.value == "recurring"
        assert sub.current_period_end is not None

    @pytest.mark.asyncio
    async def test_payload_is_never_trusted_over_the_refetch(self):
        w = World()
        payload = _load("subscription_activated")
        payload["data"]["status"] = "active"
        # Paddle's truth says canceled: the handler must act on that.
        canceled = _load("subscription_canceled")["data"]
        w.paddle.subscriptions[SUB_ID] = canceled
        raw = json.dumps(payload).encode()
        resp = w.client.post(
            WEBHOOK, content=raw, headers={"Paddle-Signature": _sign(raw)}
        )
        assert resp.json()["outcome"] == "ignored_stale"
        assert await w.sub() is None

    @pytest.mark.asyncio
    async def test_cancel_before_activate_ends_active(self):
        w = World()
        w.deliver("subscription_activated")
        assert w.me()["plan"]["status"] == "active"
        # The cancellation lands first; the late activation must not revive it.
        assert w.deliver("subscription_canceled").json()["outcome"] == "applied"
        assert w.me()["plan"]["status"] == "grace"
        late = _load("subscription_updated_renewal")
        late["event_id"] = "evt_late_activation"
        raw = json.dumps(late).encode()
        resp = w.client.post(
            WEBHOOK, content=raw, headers={"Paddle-Signature": _sign(raw)}
        )
        assert resp.json()["outcome"] == "ignored_noop"
        assert w.me()["plan"]["status"] == "grace"

    @pytest.mark.asyncio
    async def test_events_from_a_superseded_subscription_are_stale(self):
        w = World()
        w.deliver("subscription_activated")
        payload = _load("subscription_activated")
        payload["event_id"] = "evt_second_0011"
        payload["data"]["id"] = "sub_second"
        w.paddle.subscriptions["sub_second"] = payload["data"]
        raw = json.dumps(payload).encode()
        w.client.post(WEBHOOK, content=raw, headers={"Paddle-Signature": _sign(raw)})
        assert (await w.sub()).provider_ids["subscription_id"] == "sub_second"
        # The old subscription's late cancellation must not touch the new one.
        assert w.deliver("subscription_canceled").json()["outcome"] == "ignored_stale"
        assert w.me()["plan"]["status"] == "active"

    @pytest.mark.asyncio
    async def test_grace_runs_from_the_provider_cancel_date(self):
        w = World()
        w.deliver("subscription_activated")
        w.deliver("subscription_canceled")
        sub = await w.sub()
        canceled_at = datetime(2026, 10, 4, 10, 18, 47, 635628, tzinfo=timezone.utc)
        assert sub.current_period_end is not None
        assert sub.grace_until == max(
            canceled_at, sub.updated_at.replace(tzinfo=timezone.utc)
        ) + timedelta(days=14)

    @pytest.mark.asyncio
    async def test_lapsed_prepaid_customer_can_go_monthly_again(self):
        w = World()
        w.deliver("transaction_completed_prepaid")
        await w.entitlements.transition(
            USER_ID, SubscriptionEvent.ENDED, actor="test", reason="term over"
        )
        assert w.me()["plan"]["name"] == "free"
        assert w.deliver("subscription_activated").json()["outcome"] == "applied"
        sub = await w.sub()
        assert sub.kind.value == "recurring"
        assert w.me()["plan"]["status"] == "active"

    @pytest.mark.asyncio
    async def test_recurring_transaction_is_left_to_subscription_events(self):
        w = World()
        w.deliver("subscription_activated")
        resp = w.deliver("transaction_completed_recurring")
        assert resp.json()["outcome"] == "ignored_noop"

    @pytest.mark.asyncio
    async def test_prepaid_year_creates_a_prepaid_subscription(self):
        w = World()
        resp = w.deliver("transaction_completed_prepaid")
        assert resp.json()["outcome"] == "applied"
        sub = await w.sub()
        assert sub.kind.value == "prepaid"
        assert sub.status.value == "active"
        assert sub.founding is True
        days = (sub.prepaid_until - datetime.now(timezone.utc)).days
        assert 364 <= days <= 365
        assert sub.provider_ids["last_transaction_id"] == "txn_prepaid_0007"
        assert w.me()["plan"]["name"] == "pro"

    @pytest.mark.asyncio
    async def test_monthly_to_annual_schedules_the_cancel_and_prepays_from_period_end(
        self,
    ):
        w = World()
        w.deliver("subscription_activated")
        before = await w.sub()
        w.deliver("transaction_completed_prepaid")
        sub = await w.sub()
        assert sub.kind.value == "prepaid"
        assert sub.prepaid_until == before.current_period_end + timedelta(days=365)
        assert (
            f"/subscriptions/{SUB_ID}/cancel",
            {"effective_from": "next_billing_period"},
        ) in w.paddle.posts
        # The monthly subscription's later cancellation no longer touches the plan.
        assert w.deliver("subscription_canceled").json()["outcome"] == "ignored_noop"
        assert w.me()["plan"]["status"] == "active"

    @pytest.mark.asyncio
    async def test_year_on_top_of_a_scheduled_cancel_does_not_cancel_again(self):
        w = World()
        w.deliver("subscription_activated")
        w.deliver("subscription_updated_cancel_scheduled")
        w.deliver("transaction_completed_prepaid")
        assert (await w.sub()).kind.value == "prepaid"
        assert f"/subscriptions/{SUB_ID}/cancel" not in [p for p, _ in w.paddle.posts]

    @pytest.mark.asyncio
    async def test_failed_cancel_is_recorded_for_the_reconcile_to_retry(self):
        w = World()
        w.deliver("subscription_activated")
        w.paddle.fail_cancel = True
        assert w.deliver("transaction_completed_prepaid").json()["outcome"] == "applied"
        sub = await w.sub()
        assert sub.kind.value == "prepaid"
        assert sub.provider_ids["cancel_pending"] == SUB_ID
        assert [s.user_id for s in await w.subs.find_cancel_pending()] == [USER_ID]
        w.paddle.fail_cancel = False
        assert await w.billing.schedule_pending_cancel(USER_ID, SUB_ID) is True
        assert "cancel_pending" not in (await w.sub()).provider_ids
        assert await w.subs.find_cancel_pending() == []

    @pytest.mark.asyncio
    async def test_pending_cancel_clears_once_the_customer_canceled_themselves(self):
        w = World()
        w.deliver("subscription_activated")
        w.paddle.fail_cancel = True
        w.deliver("transaction_completed_prepaid")
        assert (await w.sub()).provider_ids["cancel_pending"] == SUB_ID
        # Canceled through the portal before the retry: nothing left to post.
        w.paddle.subscriptions[SUB_ID] = _load("subscription_canceled")["data"]
        posts_before = len(w.paddle.posts)
        assert await w.billing.schedule_pending_cancel(USER_ID, SUB_ID) is True
        assert "cancel_pending" not in (await w.sub()).provider_ids
        assert len(w.paddle.posts) == posts_before

    @pytest.mark.asyncio
    async def test_full_price_repurchase_does_not_heal_the_founding_streak(self):
        w = World()
        w.deliver("subscription_activated")
        w.paddle.set_from_fixture("transaction_completed_recurring")
        w.deliver("adjustment_updated_full_refund")
        assert (await w.sub()).founding_streak_ok is False
        payload = _load("subscription_activated")
        payload["event_id"] = "evt_full_price_0012"
        payload["data"]["id"] = "sub_full_price"
        payload["data"]["discount"] = None
        w.paddle.subscriptions["sub_full_price"] = payload["data"]
        raw = json.dumps(payload).encode()
        w.client.post(WEBHOOK, content=raw, headers={"Paddle-Signature": _sign(raw)})
        sub = await w.sub()
        assert sub.status.value == "active"
        assert sub.founding is True
        assert sub.founding_streak_ok is False
        w.client.post("/api/v1/billing/checkout", json={"cadence": "year"})
        assert "discount_id" not in w.paddle.posts[-1][1]

    @pytest.mark.asyncio
    async def test_redelivered_prepaid_transaction_does_not_add_a_second_year(self):
        w = World()
        w.deliver("transaction_completed_prepaid")
        until = (await w.sub()).prepaid_until
        payload = _load("transaction_completed_prepaid")
        payload["event_id"] = "evt_prepaid_redelivered"
        raw = json.dumps(payload).encode()
        resp = w.client.post(
            WEBHOOK, content=raw, headers={"Paddle-Signature": _sign(raw)}
        )
        assert resp.json()["outcome"] == "ignored_noop"
        assert (await w.sub()).prepaid_until == until

    @pytest.mark.asyncio
    async def test_full_refund_lapses_immediately(self):
        w = World()
        w.deliver("subscription_activated")
        w.paddle.set_from_fixture("transaction_completed_recurring")
        resp = w.deliver("adjustment_updated_full_refund")
        assert resp.json()["outcome"] == "applied"
        me = w.me()
        assert me["plan"]["name"] == "free"
        assert me["plan"]["status"] == "lapsed"
        assert (await w.sub()).founding_streak_ok is False

    @pytest.mark.asyncio
    async def test_scenario_checkout_failure_recovery_cancel_grace_repurchase(self):
        w = World()
        # Checkout mints a transaction with the founding discount.
        assert (
            w.client.post(
                "/api/v1/billing/checkout", json={"cadence": "monthly"}
            ).status_code
            == 200
        )
        assert w.me()["plan"]["name"] == "free"

        assert w.deliver("subscription_activated").json()["outcome"] == "applied"
        assert w.me()["plan"] | {"until": None} == {
            "name": "pro",
            "status": "active",
            "until": None,
            "founding": True,
        }

        assert w.deliver("subscription_past_due").json()["outcome"] == "applied"
        me = w.me()
        assert (me["plan"]["name"], me["plan"]["status"]) == ("pro", "past_due")
        assert me["features"]["geo_targeting"] == "enabled"

        assert w.deliver("subscription_updated_renewal").json()["outcome"] == "applied"
        me = w.me()
        assert me["plan"]["status"] == "active"
        assert me["plan"]["until"].startswith("2026-11-04")

        assert (
            w.deliver("subscription_updated_cancel_scheduled").json()["outcome"]
            == "applied"
        )
        assert w.me()["plan"]["status"] == "cancel_at_period_end"

        assert w.deliver("subscription_canceled").json()["outcome"] == "applied"
        me = w.me()
        assert (me["plan"]["name"], me["plan"]["status"]) == ("pro", "grace")
        assert me["features"]["geo_targeting"] == "enabled"

        # Repurchase during grace: a new checkout keeps the founding renewal price.
        w.client.post("/api/v1/billing/checkout", json={"cadence": "monthly"})
        assert w.paddle.posts[-1][1]["discount_id"] == "dsc_founding_renew_test"
        payload = _load("subscription_activated")
        payload["event_id"] = "evt_repurchase_0010"
        payload["data"]["id"] = "sub_second"
        w.paddle.subscriptions["sub_second"] = payload["data"]
        raw = json.dumps(payload).encode()
        resp = w.client.post(
            WEBHOOK, content=raw, headers={"Paddle-Signature": _sign(raw)}
        )
        assert resp.json()["outcome"] == "applied"
        me = w.me()
        assert (me["plan"]["name"], me["plan"]["status"]) == ("pro", "active")
        assert (await w.sub()).provider_ids["subscription_id"] == "sub_second"

        audit = await w.events._col.find({"user_id": USER_ID}).to_list()
        assert [d["kind"] for d in audit].count("grace_started") == 1
        assert me["version"] == await w.events.count_for(USER_ID)
