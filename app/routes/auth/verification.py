"""
Email verification routes.

GET  /auth/verify            → HTML template or redirect to /dashboard
POST /auth/send-verification
POST /auth/verify-email
"""

from __future__ import annotations

from bson import ObjectId
from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse

from app.dependencies import (
    AuthUser,
    JwtConfig,
    UserRepo,
    VerificationSvc,
    fetch_user_profile,
)
from app.infrastructure.templates import templates
from app.middleware.openapi import ERROR_RESPONSES
from app.middleware.rate_limiter import Limits, limiter
from app.routes.cookie_helpers import set_auth_cookies
from app.schemas.dto.requests.auth import VerifyEmailRequest
from app.schemas.dto.responses.auth import (
    SendVerificationResponse,
    VerifyEmailResponse,
)
from app.services.auth.otp import OTP_EXPIRY_SECONDS

router = APIRouter()


@router.get("/auth/verify", include_in_schema=False)
@limiter.limit(Limits.DASHBOARD_READ)
async def verify_page(
    request: Request,
    user: AuthUser,
    user_repo: UserRepo,
) -> Response:
    """Email verification page.

    Redirects to /dashboard if already verified; renders verify.html otherwise.
    """
    profile = await fetch_user_profile(user_repo, ObjectId(str(user.user_id)))
    if profile.email_verified:
        return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse(request, "verify.html", {"email": profile.email})


@router.post(
    "/auth/send-verification",
    responses=ERROR_RESPONSES,
    operation_id="sendVerification",
    summary="Send Verification Email",
)
@limiter.limit(Limits.RESEND_VERIFICATION)
async def send_verification(
    request: Request,
    user: AuthUser,
    verification_service: VerificationSvc,
) -> SendVerificationResponse:
    """Send a 6-digit OTP verification code to the user's email.

    The code expires after the duration returned in ``expires_in`` (seconds).
    Returns 400 if the user's email is already verified.

    **Authentication**: Required (JWT or API key)

    **Rate Limits**: 1/minute, 3/hour

    **Notes**: Previous unused OTPs are invalidated when a new one is sent.
    """
    await verification_service.send_verification(str(user.user_id))
    return SendVerificationResponse(
        success=True,
        message="verification code sent to your email",
        expires_in=OTP_EXPIRY_SECONDS,
    )


@router.post(
    "/auth/verify-email",
    responses=ERROR_RESPONSES,
    operation_id="verifyEmail",
    summary="Verify Email",
)
@limiter.limit(Limits.EMAIL_VERIFY)
async def verify_email(
    request: Request,
    response: Response,
    body: VerifyEmailRequest,
    user: AuthUser,
    verification_service: VerificationSvc,
    jwt_cfg: JwtConfig,
) -> VerifyEmailResponse:
    """Verify the user's email address using a 6-digit OTP code.

    On success, new JWT tokens are issued with ``email_verified=true`` in the
    claims, and auth cookies are updated. A welcome email is sent best-effort.

    **Authentication**: Required (JWT or API key)

    **Rate Limits**: 10/hour

    **Notes**: The OTP must match the most recently sent code and must not
    be expired. Expired or already-used codes are rejected.
    """
    new_access, new_refresh = await verification_service.verify_email(
        str(user.user_id), body.code.strip(), amr=user.amr
    )
    set_auth_cookies(response, new_access, new_refresh, jwt_cfg)
    return VerifyEmailResponse(
        success=True,
        message="email verified successfully",
        email_verified=True,
    )
