"""
POST /api/v1/reports + POST /api/v1/contact — report intake and contact.

The JSON twins of the Jinja ``/report`` and ``/contact`` forms (which
stay untouched until Phase 8 cleanup). Contact reuses ContactService
verbatim; reports are the bulk-first intake system — storage with
dedupe+velocity, the webhook demoted to one summary per submission.

Both endpoints 503 with ``code: not_configured`` when their webhook URL
is unset, mirroring the Jinja handlers' 503 pages. Captcha gates mirror
them too: a missing token is a 400 before any service work when
``hcaptcha_sitekey`` is configured (reports: anonymous callers only).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.dependencies import (
    REPORTS_SCOPES,
    ContactSvc,
    CurrentUser,
    ReportIntakeSvc,
    Settings,
    optional_scopes,
)
from app.errors import NotConfiguredError, ValidationError
from app.middleware.openapi import (
    NOT_CONFIGURED_RESPONSES,
    OPTIONAL_AUTH_SECURITY,
    PUBLIC_SECURITY,
)
from app.middleware.rate_limiter import Limits, dynamic_limit, limiter
from app.schemas.dto.requests.reports import ContactRequest, CreateReportsRequest
from app.schemas.dto.responses.reports import (
    ContactOkResponse,
    RejectedReportItem,
    ReportSubmissionResponse,
)
from app.shared.ip_utils import get_client_ip

router = APIRouter(tags=["Reports & Contact"])

_reports_limit, _reports_key = dynamic_limit(Limits.REPORTS_AUTHED, Limits.REPORTS_ANON)


@router.post(
    "/reports",
    responses=NOT_CONFIGURED_RESPONSES,
    openapi_extra=OPTIONAL_AUTH_SECURITY,
    operation_id="submitReports",
    summary="Report URLs",
)
@limiter.limit(_reports_limit, key_func=_reports_key)
async def submit_reports(
    request: Request,
    body: CreateReportsRequest,
    report_intake_service: ReportIntakeSvc,
    settings: Settings,
    user: CurrentUser | None = Depends(optional_scopes(REPORTS_SCOPES)),  # noqa: B008
) -> ReportSubmissionResponse:
    """Report shortened URLs for abuse — one or many per request.

    Accepts bare codes or full short URLs (custom domains included) and
    returns a per-item accepted/rejected breakdown — bad codes don't sink
    the batch. Re-reports of a code increment its counter rather than
    filing duplicates.

    **Authentication**: Optional. Anonymous submissions are captcha-gated
    and capped at **25 items/request**; authenticated callers (session or
    API key) skip the captcha and may send **100 items/request**.

    **API Key Scope**: `reports:create` or `admin:all`

    **Rate Limits** (per submission, not per item):

    - Authenticated: 30/min, 500/day
    - Anonymous: 5/min, 40/day
    """
    if not settings.url_report_webhook:
        raise NotConfiguredError("Report intake is not configured on this instance")

    if user is None and settings.hcaptcha_sitekey and not body.captcha_token:
        raise ValidationError("Please complete the captcha")

    outcome = await report_intake_service.submit(
        body.items,
        reporter_id=user.user_id if user is not None else None,
        reporter_email=body.reporter_email,
        reporter_org=body.reporter_org,
        captcha_token=body.captcha_token,
        source="api" if user is not None and user.api_key_doc is not None else "web",
        ip=get_client_ip(request),
    )
    return ReportSubmissionResponse(
        submission_id=outcome.submission_id,
        accepted=outcome.accepted,
        rejected=[
            RejectedReportItem(index=r.index, input=r.input, code=r.code)
            for r in outcome.rejected
        ],
    )


@router.post(
    "/contact",
    responses=NOT_CONFIGURED_RESPONSES,
    openapi_extra=PUBLIC_SECURITY,
    operation_id="submitContactMessage",
    summary="Contact",
)
@limiter.limit(Limits.CONTACT)
async def submit_contact(
    request: Request,
    body: ContactRequest,
    contact_service: ContactSvc,
    settings: Settings,
) -> ContactOkResponse:
    """Send a message to the site operators.

    JSON twin of the ``/contact`` form — same service, same webhook, same
    budget values.

    **Authentication**: None.

    **Rate Limits**: 5/min, 20/hour, 50/day
    """
    if not settings.contact_webhook:
        raise NotConfiguredError("Contact form is not configured on this instance")

    if settings.hcaptcha_sitekey and not body.captcha_token:
        raise ValidationError("Please complete the captcha")

    await contact_service.send_contact_message(
        body.email, body.message, body.captcha_token or ""
    )
    return ContactOkResponse()
