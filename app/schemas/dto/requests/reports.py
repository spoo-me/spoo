"""
Request DTOs for the report intake + contact API.

ContactRequest       — POST /api/v1/contact
CreateReportsRequest — POST /api/v1/reports

DTOs validate shape only (types, lengths, enums). Auth-dependent
semantics — the anonymous/authenticated item caps and the
captcha-required gate — live in the route/service layer so they can
return the contract's 400s with precise messages.
"""

from __future__ import annotations

from pydantic import EmailStr, Field

from app.schemas.dto.base import RequestBase
from app.schemas.enums.report import ReportReason, ReportVector


class ContactRequest(RequestBase):
    """Request body for POST /api/v1/contact."""

    email: EmailStr = Field(description="Sender's email address")
    message: str = Field(
        min_length=1,
        max_length=4000,
        description="Message body (1-4000 characters)",
    )
    captcha_token: str | None = Field(
        default=None,
        description="hCaptcha response token — required when captcha is configured",
    )


class ReportItemRequest(RequestBase):
    """One reported link inside POST /api/v1/reports."""

    code_or_url: str = Field(
        min_length=1,
        max_length=2048,
        description=(
            "Bare short code or full short URL — custom-domain URLs included "
            "(e.g. `abc123`, `spoo.me/abc123`, `https://go.customer.com/deal`)"
        ),
        examples=["abc123", "https://spoo.me/abc123"],
    )
    reason: ReportReason = Field(description="Reporter-claimed reason (triage hint)")
    details: str | None = Field(
        default=None,
        max_length=2000,
        description="Optional free-text context",
    )
    vector: ReportVector | None = Field(
        default=None,
        description="How the link reached the reporter",
    )


class CreateReportsRequest(RequestBase):
    """Request body for POST /api/v1/reports.

    ``items`` size is validated in the service (anonymous ≤ 25,
    authenticated ≤ 100; empty → 400) because the cap depends on the
    caller's auth state.
    """

    items: list[ReportItemRequest] = Field(
        description="Reported links — anonymous ≤ 25 per request, authenticated ≤ 100",
    )
    reporter_email: EmailStr | None = Field(
        default=None,
        description="Optional contact for resolution follow-up",
    )
    reporter_org: str | None = Field(
        default=None,
        max_length=200,
        description="Optional organisation name",
    )
    captcha_token: str | None = Field(
        default=None,
        description=(
            "hCaptcha response token — required for anonymous submissions "
            "when captcha is configured; ignored for authenticated callers"
        ),
    )
