"""
Billing event document: one per provider webhook delivery, keyed by the
provider's event id.

The unique index on ``event_id`` is the dedupe. The document is written
before the event is acted on, so a redelivery finds it and stops.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from schemas.models.base import MongoBaseModel


class BillingEventOutcome(str, Enum):
    RECEIVED = "received"
    APPLIED = "applied"
    IGNORED_STALE = "ignored_stale"
    IGNORED_NOOP = "ignored_noop"
    IGNORED_UNKNOWN = "ignored_unknown"
    FAILED = "failed"


class BillingEventDoc(MongoBaseModel):
    event_id: str
    type: str
    occurred_at: datetime | None = None
    subscription_id: str | None = None
    transaction_id: str | None = None
    payload: dict[str, Any]
    received_at: datetime
    processed_at: datetime | None = None
    outcome: BillingEventOutcome = BillingEventOutcome.RECEIVED
    detail: str | None = None
