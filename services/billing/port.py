"""
The billing provider port: the six calls the rest of the app may make.

Nothing outside ``services/billing`` knows which provider is behind it. The
provider owns money; this package only turns its facts into subscription
transitions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class Checkout:
    url: str
    transaction_id: str


@dataclass(frozen=True)
class ProviderSubscription:
    id: str
    status: str
    customer_id: str
    price_id: str | None
    custom_data: dict[str, Any] = field(default_factory=dict)
    current_period_end: datetime | None = None
    scheduled_cancel: bool = False
    canceled_at: datetime | None = None
    discount_id: str | None = None


@dataclass(frozen=True)
class ProviderTransaction:
    id: str
    status: str
    customer_id: str | None
    subscription_id: str | None
    price_ids: tuple[str, ...]
    custom_data: dict[str, Any] = field(default_factory=dict)
    billed_at: datetime | None = None
    discount_id: str | None = None


@dataclass(frozen=True)
class ProviderAdjustment:
    id: str
    action: str
    type: str
    status: str
    transaction_id: str
    subscription_id: str | None
    customer_id: str | None


@dataclass(frozen=True)
class ProviderDiscount:
    id: str
    times_used: int
    usage_limit: int | None
    expires_at: datetime | None


@dataclass(frozen=True)
class WebhookEvent:
    event_id: str
    event_type: str
    occurred_at: datetime | None
    data: dict[str, Any]


class BillingProvider(Protocol):
    name: str

    async def create_checkout(
        self,
        *,
        price_id: str,
        discount_id: str | None,
        custom_data: dict[str, Any],
        checkout_url: str,
    ) -> Checkout: ...

    async def portal_url(
        self, customer_id: str, subscription_ids: list[str]
    ) -> str: ...

    def verify_webhook(self, raw: bytes, signature: str | None) -> WebhookEvent: ...

    async def fetch_subscription(
        self, subscription_id: str
    ) -> ProviderSubscription: ...

    async def fetch_transaction(self, transaction_id: str) -> ProviderTransaction: ...

    async def fetch_adjustment(self, adjustment_id: str) -> ProviderAdjustment: ...

    async def schedule_cancel(self, subscription_id: str) -> None: ...

    async def fetch_discount(self, discount_id: str) -> ProviderDiscount: ...
