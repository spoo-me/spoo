"""
GET /api/v1/plans — public projection of the feature catalog with display prices.

The pricing page and the upgrade page both read this, so marketing copy and
enforcement come from the same table.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from dependencies import Settings
from middleware.rate_limiter import Limits, limiter
from schemas.dto.responses.entitlements import (
    FoundingBlock,
    PlanEntry,
    PlansResponse,
    PriceBlock,
)
from services.features.catalog import Plan, bool_features, int_features

router = APIRouter(prefix="/plans", tags=["Plans"])

_PUBLIC_PLANS = (Plan.FREE, Plan.PRO)
_CURRENCY = "USD"


@router.get(
    "",
    operation_id="listPlans",
    summary="List Plans",
)
@limiter.limit(Limits.API_ANON)
async def list_plans(request: Request, settings: Settings) -> PlansResponse:
    """Return the free and pro plans with their features, limits and prices.

    A self-hosted deployment has nothing to sell: `prices` is empty and
    `founding` is null there.

    **Authentication**: Not required.
    """
    billing = settings.billing
    plans = [
        PlanEntry(
            name=plan.value,
            features={f.key: bool(f.plans[plan]) for f in bool_features()},
            limits={f.key: int(f.plans[plan]) for f in int_features()},
        )
        for plan in _PUBLIC_PLANS
    ]
    if billing.selfhost:
        return PlansResponse(plans=plans)
    return PlansResponse(
        plans=plans,
        prices={
            "monthly": PriceBlock(amount=billing.pro_monthly_usd, currency=_CURRENCY),
            "year": PriceBlock(amount=billing.pro_year_usd, currency=_CURRENCY),
        },
        founding=FoundingBlock(
            monthly=PriceBlock(amount=billing.founding_monthly_usd, currency=_CURRENCY),
            year=PriceBlock(amount=billing.founding_year_usd, currency=_CURRENCY),
            seats_total=billing.founding_seats,
        ),
    )
