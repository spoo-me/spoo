"""Response DTOs for GET /api/v1/me/entitlements and GET /api/v1/plans."""

from __future__ import annotations

from pydantic import Field

from schemas.dto.base import ResponseBase, UtcDatetime
from schemas.enums.feature_state import FeatureState


class PlanBlock(ResponseBase):
    name: str = Field(description="Effective plan", examples=["pro"])
    status: str | None = Field(
        default=None,
        description="Subscription status; null on the free plan",
        examples=["active"],
    )
    until: UtcDatetime | None = Field(
        default=None,
        description="Term end, or the grace end while in grace; null when nothing ends",
    )
    founding: bool = Field(default=False, description="Founding cohort member")


class LimitBlock(ResponseBase):
    max: int = Field(description="Plan maximum; -1 means unlimited", examples=[5])
    used: int | None = Field(
        default=None,
        description="Live count for countable limits; null for windows and rates",
        examples=[2],
    )


class OverLimitBlock(ResponseBase):
    paused: list[str] = Field(
        description="Ids of items paused because the limit shrank below them"
    )


class EntitlementsResponse(ResponseBase):
    """Everything the dashboard needs to render plan state in one call.

    ``version`` changes on every effective subscription or override write;
    clients poll it after checkout and compare it with the
    ``X-Entitlements-Version`` header on every authenticated response.
    """

    version: int = Field(description="Owner entitlement version", examples=[3])
    plan: PlanBlock
    features: dict[str, FeatureState] = Field(
        description="Per-feature UI state; treat missing keys as hidden"
    )
    limits: dict[str, LimitBlock]
    over_limit: dict[str, OverLimitBlock] = Field(default_factory=dict)


class PlanEntry(ResponseBase):
    name: str = Field(examples=["pro"])
    features: dict[str, bool]
    limits: dict[str, int] = Field(description="-1 means unlimited")


class PriceBlock(ResponseBase):
    amount: int = Field(description="Whole units", examples=[15])
    currency: str = Field(examples=["USD"])


class FoundingBlock(ResponseBase):
    monthly: PriceBlock
    year: PriceBlock
    seats_total: int
    seats_left: int | None = Field(
        default=None, description="Null until billing reports the count"
    )
    until: UtcDatetime | None = Field(
        default=None, description="Null until the checkout window opens"
    )


class PlansResponse(ResponseBase):
    """Public projection of the feature catalog plus display prices."""

    plans: list[PlanEntry]
    prices: dict[str, PriceBlock] = Field(
        default_factory=dict,
        description="Keyed by cadence: monthly (recurring), year (one-time); "
        "empty on a self-hosted deployment",
    )
    founding: FoundingBlock | None = None
