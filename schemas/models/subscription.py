"""
Subscription document model: the ``subscriptions`` collection, one per user.

A missing document means the free plan. ``status`` is the only fact readers
use to decide the plan; dates exist for the lifecycle job and the UI, and no
request-path code does date math on them.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import Field

from schemas.enums.plan import Plan
from schemas.models.base import MongoBaseModel, PyObjectId


class SubscriptionStatus(str, Enum):
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCEL_AT_PERIOD_END = "cancel_at_period_end"
    GRACE = "grace"
    LAPSED = "lapsed"


PRO_STATUSES: frozenset[SubscriptionStatus] = frozenset(
    {
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.PAST_DUE,
        SubscriptionStatus.CANCEL_AT_PERIOD_END,
        SubscriptionStatus.GRACE,
    }
)


class SubscriptionKind(str, Enum):
    RECURRING = "recurring"
    PREPAID = "prepaid"


class SubscriptionProvider(str, Enum):
    PADDLE = "paddle"
    MANUAL = "manual"


class SubscriptionDoc(MongoBaseModel):
    user_id: PyObjectId
    provider: SubscriptionProvider
    provider_ids: dict[str, str] = Field(default_factory=dict)
    plan: str = Plan.PRO.value
    kind: SubscriptionKind
    status: SubscriptionStatus
    current_period_end: datetime | None = None
    prepaid_until: datetime | None = None
    grace_until: datetime | None = None
    founding: bool = False
    founding_streak_ok: bool = False
    # The state-machine event that lapsed it; "ended" means a refund or a
    # manual end, not a term running out.
    lapsed_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def effective_plan(self) -> Plan:
        return Plan.PRO if self.status in PRO_STATUSES else Plan.FREE

    @property
    def term_end(self) -> datetime | None:
        """When the paid term stops: the prepaid end, or the current period end."""
        if self.kind is SubscriptionKind.PREPAID:
            return self.prepaid_until
        return self.current_period_end

    @property
    def until(self) -> datetime | None:
        """The date the UI shows next to the status."""
        if self.status is SubscriptionStatus.GRACE:
            return self.grace_until
        return self.term_end
