"""Webhook document models.

Three collections:
- ``webhook-endpoints``  — subscriptions + signing material + health
- ``webhook-events``     — the fact, stored ONCE per occurrence
- ``webhook-deliveries`` — thin per-endpoint delivery state referencing the event

The ``whsec_…`` signing secret is shown once at creation and stored
AES-GCM encrypted (never plaintext, never hash-only — the server signs
with it at delivery time, so it must be readable back).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.schemas.enums.webhook import (
    DeliveryStatus,
    EndpointDisabledReason,
    WebhookFlavor,
    WebhookStatus,
)
from app.schemas.models.base import MongoBaseModel, PyObjectId


class WebhookScope(BaseModel):
    """Subscription scope. ``links: "all"`` is the absence of a filter —
    live, includes future links. Object (not a bare list) so ``groups``
    slots in when Link Groups land. Stale link ids are inert."""

    links: Literal["all"] | list[PyObjectId] = "all"

    def matches_link(self, link_id: PyObjectId | None) -> bool:
        if self.links == "all":
            return True
        return link_id is not None and link_id in self.links


class WebhookEndpointDoc(MongoBaseModel):
    """Document model for the ``webhook-endpoints`` collection."""

    user_id: PyObjectId
    url: str
    description: str | None = None
    events: list[str] = []  # noqa: RUF012 — patterns, stored verbatim; expanded at match time
    scope: WebhookScope = WebhookScope()
    flavor: WebhookFlavor = WebhookFlavor.RAW
    status: WebhookStatus = WebhookStatus.ACTIVE
    disabled_reason: EndpointDisabledReason | None = None

    # AES-GCM encrypted at rest (infrastructure/crypto.py) — NOT hashed:
    # the server SIGNS with this secret at delivery time, so it must be
    # readable back. Prefix stored separately for display.
    signing_secret_enc: str = ""
    signing_secret_prefix: str = ""
    previous_secret_enc: str | None = None
    previous_secret_expires_at: datetime | None = None

    # Health (denormalized; counters updated atomically by the repository).
    # consecutive_failures counts EXHAUSTED deliveries, not attempts.
    consecutive_failures: int = 0
    dropped_count: int = 0  # pending-cap drops since last successful delivery
    pending_count: int = 0  # reserved at dispatch, released on every terminal outcome
    total_deliveries: int = 0
    total_successes: int = 0
    last_delivery_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_reason: str | None = None

    created_at: datetime | None = None
    updated_at: datetime | None = None


class WebhookEventDoc(MongoBaseModel):
    """Document model for the ``webhook-events`` collection — one row per
    fact regardless of fan-out — deliveries reference it. TTL-bounded."""

    event_id: str  # evt_…
    type: str
    v: int = 1
    owner_id: PyObjectId
    occurred_at: datetime
    payload: dict[str, Any]
    is_test: bool = False
    created_at: datetime | None = None  # TTL anchor


class DeliveryAttempt(BaseModel):
    model_config = ConfigDict(frozen=True)

    attempted_at: datetime
    status_code: int | None = None
    duration_ms: int | None = None
    error: str | None = None
    response_body: str | None = None  # first 256 bytes


class WebhookDeliveryDoc(MongoBaseModel):
    """Document model for the ``webhook-deliveries`` collection.

    Thin per-endpoint state: payload lives on the event row; the rendered
    body is set at FIRST attempt and frozen across retries so
    consumers can dedup on ``webhook_id`` against identical bytes."""

    endpoint_id: PyObjectId
    user_id: PyObjectId  # denormalized for owner queries
    event_oid: PyObjectId  # -> webhook-events._id
    event_type: str  # denormalized: delivery-log filtering without a join
    webhook_id: str  # msg_… — STABLE across retries (consumer dedup key)
    is_test: bool = False
    rendered_body: str | None = None
    dropped_since_last: int = 0
    status: DeliveryStatus = DeliveryStatus.PENDING
    attempts: list[DeliveryAttempt] = []  # noqa: RUF012
    attempt_count: int = 0
    next_attempt_at: datetime | None = None
    claimed_until: datetime | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None
