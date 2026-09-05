"""
POST /api/v1/billing/checkout         — mint a checkout link for Pro
POST /api/v1/billing/portal           — mint a customer portal link
POST /api/v1/billing/webhooks/paddle  — provider webhook intake (unauthenticated, signed)
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from dependencies import BillingSvc, JwtUser, JwtVerifiedUser
from middleware.openapi import AUTH_RESPONSES
from middleware.rate_limiter import Limits, limiter
from schemas.dto.requests.billing import CheckoutRequest
from schemas.dto.responses.billing import (
    CheckoutResponse,
    PortalResponse,
    WebhookAckResponse,
)

router = APIRouter(prefix="/billing", tags=["Billing"])


@router.post(
    "/checkout",
    responses=AUTH_RESPONSES,
    operation_id="createCheckout",
    summary="Start Checkout",
)
@limiter.limit(Limits.BILLING_WRITE)
async def create_checkout(
    request: Request,
    body: CheckoutRequest,
    user: JwtVerifiedUser,
    billing: BillingSvc,
) -> CheckoutResponse:
    """Create a checkout for the Pro plan and return where to send the browser.

    The server decides founding-cohort eligibility and attaches the discount;
    no code is ever shown to the user. `return` is the dashboard path to land
    on once the payment is confirmed.

    **Authentication**: Required (interactive session, verified email).
    """
    url = await billing.checkout(
        user.user_id,
        body.cadence,
        from_=body.from_feature,
        return_to=body.return_to,
    )
    return CheckoutResponse(url=url)


@router.post(
    "/portal",
    responses=AUTH_RESPONSES,
    operation_id="createPortalSession",
    summary="Open Billing Portal",
)
@limiter.limit(Limits.BILLING_WRITE)
async def create_portal(
    request: Request,
    user: JwtUser,
    billing: BillingSvc,
) -> PortalResponse:
    """Return an authenticated link to the billing portal (invoices, payment
    method, cancellation).

    **Authentication**: Required (interactive session).
    """
    return PortalResponse(url=await billing.portal(user.user_id))


@router.post(
    "/webhooks/paddle",
    include_in_schema=False,
    operation_id="paddleWebhook",
)
@limiter.exempt
async def paddle_webhook(request: Request, billing: BillingSvc) -> WebhookAckResponse:
    raw = await request.body()
    outcome = await billing.handle_webhook(raw, request.headers.get("Paddle-Signature"))
    return WebhookAckResponse(outcome=outcome)
