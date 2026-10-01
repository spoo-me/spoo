"""No billing: self-host deployments. Every money call answers 503."""

from __future__ import annotations

from typing import Any

from errors import AuthenticationError, NotConfiguredError
from services.billing.port import (
    Checkout,
    ProviderAdjustment,
    ProviderDiscount,
    ProviderSubscription,
    ProviderTransaction,
    WebhookEvent,
)

_MESSAGE = "billing is not configured on this instance"


class NullBillingProvider:
    name = "none"

    async def create_checkout(
        self,
        *,
        price_id: str,
        discount_id: str | None,
        custom_data: dict[str, Any],
        checkout_url: str,
    ) -> Checkout:
        raise NotConfiguredError(_MESSAGE)

    async def portal_url(self, customer_id: str, subscription_ids: list[str]) -> str:
        raise NotConfiguredError(_MESSAGE)

    def verify_webhook(self, raw: bytes, signature: str | None) -> WebhookEvent:
        raise AuthenticationError("no webhook provider configured")

    async def fetch_subscription(self, subscription_id: str) -> ProviderSubscription:
        raise NotConfiguredError(_MESSAGE)

    async def fetch_transaction(self, transaction_id: str) -> ProviderTransaction:
        raise NotConfiguredError(_MESSAGE)

    async def fetch_adjustment(self, adjustment_id: str) -> ProviderAdjustment:
        raise NotConfiguredError(_MESSAGE)

    async def schedule_cancel(self, subscription_id: str) -> None:
        raise NotConfiguredError(_MESSAGE)

    async def fetch_discount(self, discount_id: str) -> ProviderDiscount:
        raise NotConfiguredError(_MESSAGE)
