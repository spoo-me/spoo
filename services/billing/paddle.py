"""
Paddle Billing behind the port.

Talks to ``api.paddle.com`` or ``sandbox-api.paddle.com`` with a bearer API
key and verifies webhooks with the notification secret. Every API error is a
``BillingProviderError`` so callers never mistake a transport failure for a
plan state.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime
from typing import Any

import httpx

from errors import AuthenticationError, BillingProviderError
from infrastructure.logging import get_logger
from services.billing.port import (
    Checkout,
    ProviderAdjustment,
    ProviderDiscount,
    ProviderSubscription,
    ProviderTransaction,
    WebhookEvent,
)
from shared.datetime_utils import parse_datetime

log = get_logger(__name__)

_BASE_URLS = {
    "sandbox": "https://sandbox-api.paddle.com",
    "production": "https://api.paddle.com",
}
# Paddle re-signs every retry with a fresh timestamp, so a replay window this
# short costs nothing; it only has to absorb clock skew.
_SIGNATURE_TOLERANCE_SECONDS = 60


def _dt(value: Any) -> datetime | None:
    return parse_datetime(value) if value else None


class PaddleProvider:
    name = "paddle"

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        api_key: str,
        webhook_secret: str,
        env: str = "sandbox",
        now: Any = time.time,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._secret = webhook_secret.encode()
        self._base = _BASE_URLS[env]
        self._now = now

    # ── HTTP ─────────────────────────────────────────────────────────────

    async def _call(self, method: str, path: str, json: dict | None = None) -> dict:
        try:
            resp = await self._client.request(
                method,
                f"{self._base}{path}",
                json=json,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            log.error("paddle_request_failed", method=method, path=path, error=str(exc))
            raise BillingProviderError("billing provider unreachable") from exc
        if resp.status_code >= 400:
            log.error(
                "paddle_request_rejected",
                method=method,
                path=path,
                status=resp.status_code,
                body=resp.text[:500],
            )
            raise BillingProviderError(f"billing provider answered {resp.status_code}")
        body = resp.json()
        return body.get("data", body)

    # ── Port ─────────────────────────────────────────────────────────────

    async def create_checkout(
        self,
        *,
        price_id: str,
        discount_id: str | None,
        custom_data: dict[str, Any],
        checkout_url: str,
    ) -> Checkout:
        payload: dict[str, Any] = {
            "items": [{"price_id": price_id, "quantity": 1}],
            "custom_data": custom_data,
            "checkout": {"url": checkout_url},
        }
        if discount_id:
            payload["discount_id"] = discount_id
        data = await self._call("POST", "/transactions", payload)
        url = (data.get("checkout") or {}).get("url")
        if not url:
            raise BillingProviderError("billing provider returned no checkout url")
        return Checkout(url=url, transaction_id=data["id"])

    async def portal_url(self, customer_id: str, subscription_ids: list[str]) -> str:
        data = await self._call(
            "POST",
            f"/customers/{customer_id}/portal-sessions",
            {"subscription_ids": subscription_ids} if subscription_ids else {},
        )
        try:
            return data["urls"]["general"]["overview"]
        except (KeyError, TypeError) as exc:
            raise BillingProviderError(
                "billing provider returned no portal url"
            ) from exc

    def verify_webhook(self, raw: bytes, signature: str | None) -> WebhookEvent:
        if not signature:
            raise AuthenticationError("missing webhook signature")
        ts, h1s = None, []
        for part in signature.split(";"):
            name, sep, value = part.partition("=")
            if sep and name == "ts":
                ts = value
            elif sep and name == "h1":
                h1s.append(value)
        if not ts or not h1s or not ts.isdigit():
            raise AuthenticationError("malformed webhook signature")
        if abs(self._now() - int(ts)) > _SIGNATURE_TOLERANCE_SECONDS:
            raise AuthenticationError("webhook signature timestamp out of range")
        expected = hmac.new(
            self._secret, f"{ts}:".encode() + raw, hashlib.sha256
        ).hexdigest()
        if not any(hmac.compare_digest(expected, sig) for sig in h1s):
            raise AuthenticationError("webhook signature mismatch")
        try:
            body = json.loads(raw)
            return WebhookEvent(
                event_id=body["event_id"],
                event_type=body["event_type"],
                occurred_at=_dt(body.get("occurred_at")),
                data=body["data"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AuthenticationError("malformed webhook body") from exc

    async def fetch_subscription(self, subscription_id: str) -> ProviderSubscription:
        data = await self._call("GET", f"/subscriptions/{subscription_id}")
        return subscription_from_paddle(data)

    async def fetch_transaction(self, transaction_id: str) -> ProviderTransaction:
        data = await self._call("GET", f"/transactions/{transaction_id}")
        return transaction_from_paddle(data)

    async def fetch_adjustment(self, adjustment_id: str) -> ProviderAdjustment:
        data = await self._call("GET", f"/adjustments/{adjustment_id}")
        return adjustment_from_paddle(data)

    async def schedule_cancel(self, subscription_id: str) -> None:
        await self._call(
            "POST",
            f"/subscriptions/{subscription_id}/cancel",
            {"effective_from": "next_billing_period"},
        )

    async def fetch_discount(self, discount_id: str) -> ProviderDiscount:
        data = await self._call("GET", f"/discounts/{discount_id}")
        return ProviderDiscount(
            id=data["id"],
            times_used=int(data.get("times_used") or 0),
            usage_limit=data.get("usage_limit"),
            expires_at=_dt(data.get("expires_at")),
        )


# ── Entity mapping (shared by the API client and the webhook fixtures) ─────


def subscription_from_paddle(data: dict[str, Any]) -> ProviderSubscription:
    items = data.get("items") or []
    price_id = None
    if items:
        price_id = (items[0].get("price") or {}).get("id")
    period = data.get("current_billing_period") or {}
    change = data.get("scheduled_change") or {}
    discount = data.get("discount") or {}
    return ProviderSubscription(
        id=data["id"],
        status=data["status"],
        customer_id=data["customer_id"],
        price_id=price_id,
        custom_data=data.get("custom_data") or {},
        current_period_end=_dt(period.get("ends_at")),
        scheduled_cancel=change.get("action") == "cancel",
        canceled_at=_dt(data.get("canceled_at")),
        discount_id=discount.get("id"),
    )


def transaction_from_paddle(data: dict[str, Any]) -> ProviderTransaction:
    price_ids = tuple(
        (item.get("price") or {}).get("id")
        for item in data.get("items") or []
        if (item.get("price") or {}).get("id")
    )
    return ProviderTransaction(
        id=data["id"],
        status=data["status"],
        customer_id=data.get("customer_id"),
        subscription_id=data.get("subscription_id"),
        price_ids=price_ids,
        custom_data=data.get("custom_data") or {},
        billed_at=_dt(data.get("billed_at")),
        discount_id=data.get("discount_id"),
    )


def adjustment_from_paddle(data: dict[str, Any]) -> ProviderAdjustment:
    return ProviderAdjustment(
        id=data["id"],
        action=data["action"],
        type=data.get("type", ""),
        status=data.get("status", ""),
        transaction_id=data["transaction_id"],
        subscription_id=data.get("subscription_id"),
        customer_id=data.get("customer_id"),
    )
