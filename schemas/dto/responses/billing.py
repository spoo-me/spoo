"""Response DTOs for /api/v1/billing."""

from __future__ import annotations

from pydantic import Field

from schemas.dto.base import ResponseBase


class CheckoutResponse(ResponseBase):
    url: str = Field(description="Where to send the browser to pay")


class PortalResponse(ResponseBase):
    url: str = Field(description="Authenticated customer portal link")


class WebhookAckResponse(ResponseBase):
    outcome: str = Field(
        description="applied, duplicate, ignored_stale, ignored_noop or ignored_unknown"
    )
