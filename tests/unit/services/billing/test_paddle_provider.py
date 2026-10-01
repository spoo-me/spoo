"""PaddleProvider: signature verification and the API calls, against a
recorded transport so every request and response shape is pinned."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import httpx
import pytest

from errors import AuthenticationError, BillingProviderError
from services.billing.paddle import PaddleProvider

SECRET = "pdl_ntfset_test_secret"
NOW = 1_800_000_000
FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "paddle"


def sign(raw: bytes, *, ts: int = NOW, secret: str = SECRET) -> str:
    h1 = hmac.new(secret.encode(), f"{ts}:".encode() + raw, hashlib.sha256).hexdigest()
    return f"ts={ts};h1={h1}"


def _provider(handler=None, **kw) -> PaddleProvider:
    transport = httpx.MockTransport(handler or (lambda r: httpx.Response(500)))
    return PaddleProvider(
        httpx.AsyncClient(transport=transport),
        api_key="pdl_sdbx_apikey_test",
        webhook_secret=SECRET,
        env="sandbox",
        now=lambda: NOW,
        **kw,
    )


def _fixture(name: str) -> bytes:
    return (FIXTURES / f"{name}.json").read_bytes()


class TestSignature:
    def test_valid_signature_yields_the_event(self):
        raw = _fixture("subscription_activated")
        event = _provider().verify_webhook(raw, sign(raw))
        assert event.event_id == "evt_activated_0001"
        assert event.event_type == "subscription.activated"
        assert event.data["id"] == "sub_01hv8x29kz0t586xy6zn1a62ny"
        assert event.occurred_at is not None

    def test_missing_header_rejected(self):
        with pytest.raises(AuthenticationError):
            _provider().verify_webhook(b"{}", None)

    def test_wrong_secret_rejected(self):
        raw = _fixture("subscription_activated")
        with pytest.raises(AuthenticationError):
            _provider().verify_webhook(raw, sign(raw, secret="other"))

    def test_tampered_body_rejected(self):
        raw = _fixture("subscription_activated")
        sig = sign(raw)
        with pytest.raises(AuthenticationError):
            _provider().verify_webhook(raw + b" ", sig)

    def test_stale_timestamp_rejected(self):
        raw = _fixture("subscription_activated")
        with pytest.raises(AuthenticationError):
            _provider().verify_webhook(raw, sign(raw, ts=NOW - 3600))

    def test_rotated_secret_second_h1_accepted(self):
        raw = _fixture("subscription_activated")
        old = hmac.new(b"old", f"{NOW}:".encode() + raw, hashlib.sha256).hexdigest()
        new = hmac.new(
            SECRET.encode(), f"{NOW}:".encode() + raw, hashlib.sha256
        ).hexdigest()
        event = _provider().verify_webhook(raw, f"ts={NOW};h1={old};h1={new}")
        assert event.event_id == "evt_activated_0001"

    def test_malformed_header_rejected(self):
        with pytest.raises(AuthenticationError):
            _provider().verify_webhook(b"{}", "nonsense")


class TestApiCalls:
    @pytest.mark.asyncio
    async def test_create_checkout_posts_a_transaction_and_returns_its_url(self):
        seen = {}

        def handler(request: httpx.Request):
            seen["url"] = str(request.url)
            seen["auth"] = request.headers["Authorization"]
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                201,
                json={
                    "data": {
                        "id": "txn_123",
                        "status": "ready",
                        "checkout": {
                            "url": "https://spoo.me/upgrade/checkout?_ptxn=txn_123"
                        },
                    }
                },
            )

        checkout = await _provider(handler).create_checkout(
            price_id="pri_m",
            discount_id="dsc_f",
            custom_data={"user_id": "u1"},
            checkout_url="https://spoo.me/upgrade/checkout?return=/x",
        )
        assert seen["url"] == "https://sandbox-api.paddle.com/transactions"
        assert seen["auth"] == "Bearer pdl_sdbx_apikey_test"
        assert seen["body"] == {
            "items": [{"price_id": "pri_m", "quantity": 1}],
            "custom_data": {"user_id": "u1"},
            "checkout": {"url": "https://spoo.me/upgrade/checkout?return=/x"},
            "discount_id": "dsc_f",
        }
        assert checkout.transaction_id == "txn_123"
        assert checkout.url.endswith("_ptxn=txn_123")

    @pytest.mark.asyncio
    async def test_checkout_without_discount_omits_the_key(self):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                201, json={"data": {"id": "t", "checkout": {"url": "https://x/y"}}}
            )

        await _provider(handler).create_checkout(
            price_id="p", discount_id=None, custom_data={}, checkout_url="https://x"
        )
        assert "discount_id" not in seen["body"]

    @pytest.mark.asyncio
    async def test_portal_session_returns_the_overview_url(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                201,
                json={"data": {"urls": {"general": {"overview": "https://portal/x"}}}},
            )

        url = await _provider(handler).portal_url("ctm_1", ["sub_1"])
        assert url == "https://portal/x"
        assert seen["url"].endswith("/customers/ctm_1/portal-sessions")
        assert seen["body"] == {"subscription_ids": ["sub_1"]}

    @pytest.mark.asyncio
    async def test_fetch_subscription_maps_the_entity(self):
        data = json.loads(_fixture("subscription_updated_cancel_scheduled"))["data"]

        def handler(request):
            assert str(request.url).endswith(
                "/subscriptions/sub_01hv8x29kz0t586xy6zn1a62ny"
            )
            return httpx.Response(200, json={"data": data})

        sub = await _provider(handler).fetch_subscription(
            "sub_01hv8x29kz0t586xy6zn1a62ny"
        )
        assert sub.status == "active"
        assert sub.scheduled_cancel is True
        assert sub.price_id == "pri_pro_monthly_test"
        assert sub.customer_id == "ctm_01hv6y1jedq4p1n0yqn5ba3ky4"
        assert sub.custom_data["user_id"] == "aaaaaaaaaaaaaaaaaaaaaaaa"
        assert sub.current_period_end is not None
        assert sub.discount_id == "dsc_founding_first_test"

    @pytest.mark.asyncio
    async def test_fetch_transaction_maps_prices_and_subscription(self):
        data = json.loads(_fixture("transaction_completed_prepaid"))["data"]
        tx = await _provider(
            lambda r: httpx.Response(200, json={"data": data})
        ).fetch_transaction("txn_prepaid_0007")
        assert tx.subscription_id is None
        assert tx.price_ids == ("pri_pro_year_test",)
        assert tx.status == "completed"
        assert tx.custom_data["cadence"] == "year"

    @pytest.mark.asyncio
    async def test_schedule_cancel_posts_next_billing_period(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"data": {"id": "sub_1"}})

        await _provider(handler).schedule_cancel("sub_1")
        assert seen["url"].endswith("/subscriptions/sub_1/cancel")
        assert seen["body"] == {"effective_from": "next_billing_period"}

    @pytest.mark.asyncio
    async def test_fetch_discount_maps_usage(self):
        data = {
            "id": "dsc_1",
            "times_used": 37,
            "usage_limit": 100,
            "expires_at": "2026-12-03T00:00:00Z",
        }
        discount = await _provider(
            lambda r: httpx.Response(200, json={"data": data})
        ).fetch_discount("dsc_1")
        assert (discount.times_used, discount.usage_limit) == (37, 100)
        assert discount.expires_at is not None

    @pytest.mark.asyncio
    async def test_provider_errors_become_billing_provider_error(self):
        with pytest.raises(BillingProviderError):
            await _provider(
                lambda r: httpx.Response(403, json={"error": "nope"})
            ).fetch_subscription("s")

        def boom(request):
            raise httpx.ConnectError("down")

        with pytest.raises(BillingProviderError):
            await _provider(boom).fetch_subscription("s")

    @pytest.mark.asyncio
    async def test_production_env_uses_the_live_host(self):
        seen = {}

        def handler(request):
            seen["host"] = request.url.host
            return httpx.Response(
                200, json={"data": {"id": "s", "status": "active", "customer_id": "c"}}
            )

        provider = PaddleProvider(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            api_key="k",
            webhook_secret="s",
            env="production",
        )
        await provider.fetch_subscription("s")
        assert seen["host"] == "api.paddle.com"
