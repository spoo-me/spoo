"""
Password reset routes.

POST /auth/request-password-reset
POST /auth/reset-password
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.dependencies import PasswordSvc
from app.middleware.openapi import ERROR_RESPONSES, PUBLIC_SECURITY
from app.middleware.rate_limiter import Limits, limiter
from app.schemas.dto.requests.auth import (
    RequestPasswordResetRequest,
    ResetPasswordRequest,
)
from app.schemas.dto.responses.common import MessageResponse

router = APIRouter()


@router.post(
    "/auth/request-password-reset",
    openapi_extra=PUBLIC_SECURITY,
    operation_id="requestPasswordReset",
    summary="Request Password Reset",
)
@limiter.limit(Limits.PASSWORD_RESET_REQUEST)
async def request_password_reset(
    request: Request,
    body: RequestPasswordResetRequest,
    password_service: PasswordSvc,
) -> MessageResponse:
    """Request a password-reset OTP to be sent via email.

    Always returns the same success response regardless of whether the
    email is registered. This prevents user enumeration attacks.

    **Authentication**: Not required (public endpoint)

    **Rate Limits**: 3/hour

    **Security**: Timing-safe -- response time is constant whether or not
    the account exists.
    """
    await password_service.request_password_reset(body.email.strip().lower())
    return MessageResponse(
        success=True, message="if the email exists, a reset code has been sent"
    )


@router.post(
    "/auth/reset-password",
    responses=ERROR_RESPONSES,
    openapi_extra=PUBLIC_SECURITY,
    operation_id="resetPassword",
    summary="Reset Password",
)
@limiter.limit(Limits.PASSWORD_RESET_CONFIRM)
async def reset_password(
    request: Request,
    body: ResetPasswordRequest,
    password_service: PasswordSvc,
) -> MessageResponse:
    """Reset the account password using a 6-digit OTP code.

    The OTP must have been requested via ``POST /auth/request-password-reset``.
    On success the password is updated immediately and the OTP is consumed.

    **Authentication**: Not required (public endpoint)

    **Rate Limits**: 5/hour

    **Notes**: Expired or already-used OTPs are rejected with a 400 error.
    """
    await password_service.reset_password(
        body.email.strip().lower(), body.code.strip(), body.password
    )
    return MessageResponse(success=True, message="password reset successfully")
