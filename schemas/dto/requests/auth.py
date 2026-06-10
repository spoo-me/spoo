"""
Request DTOs for authentication endpoints.

LoginRequest                  — POST /auth/login
RegisterRequest               — POST /auth/register
SetPasswordRequest            — POST /auth/set-password
VerifyEmailRequest            — POST /auth/verify-email
SendVerificationRequest       — POST /auth/send-verification  (no body)
RequestPasswordResetRequest   — POST /auth/request-password-reset
ResetPasswordRequest          — POST /auth/reset-password
DeviceTokenRequest            — POST /auth/device/token
DeviceRefreshRequest          — POST /auth/device/refresh
"""

from __future__ import annotations

from typing import Literal

from pydantic import EmailStr, Field

from schemas.dto.base import RequestBase


class LoginRequest(RequestBase):
    """Request body for POST /auth/login."""

    email: EmailStr = Field(
        description="Account email address", examples=["user@example.com"]
    )
    password: str = Field(
        max_length=255,
        description="Account password",
        examples=["MySecurePass123!"],
    )


class RegisterRequest(RequestBase):
    """Request body for POST /auth/register."""

    email: EmailStr = Field(
        description="Email address for the new account",
        examples=["newuser@example.com"],
    )
    password: str = Field(
        max_length=128,
        description="Password (min 8 chars, must contain letter + number + special char)",
        examples=["MySecurePass123!"],
    )
    user_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        description="Display name (optional)",
        examples=["Jane Doe"],
    )


class SetPasswordRequest(RequestBase):
    """Request body for POST /auth/set-password.

    Only applies to OAuth-only users who have not yet set a password.
    """

    password: str = Field(
        max_length=128,
        description="New password (min 8 chars, must contain letter + number + special char)",
        examples=["MySecurePass123!"],
    )


class VerifyEmailRequest(RequestBase):
    """Request body for POST /auth/verify-email.

    ``code`` is the 6-digit OTP sent to the user's email address.
    """

    code: str = Field(
        pattern=r"^[0-9]{6}$",
        description="6-digit OTP from verification email",
        examples=["123456"],
    )


class RequestPasswordResetRequest(RequestBase):
    """Request body for POST /auth/request-password-reset."""

    email: EmailStr = Field(
        description="Email address of the account to reset",
        examples=["user@example.com"],
    )


class ResetPasswordRequest(RequestBase):
    """Request body for POST /auth/reset-password."""

    email: EmailStr = Field(
        description="Email address of the account",
        examples=["user@example.com"],
    )
    code: str = Field(
        pattern=r"^[0-9]{6}$",
        description="6-digit OTP from password reset email",
        examples=["123456"],
    )
    password: str = Field(
        max_length=128,
        description="New password (min 8 chars, must contain letter + number + special char)",
        examples=["NewSecurePass456!"],
    )


class DeviceTokenRequest(RequestBase):
    """Request body for POST /auth/device/token."""

    code: str = Field(
        min_length=1,
        max_length=128,
        description="One-time auth code from the device callback page",
    )


class DeviceRefreshRequest(RequestBase):
    """Request body for POST /auth/device/refresh."""

    refresh_token: str = Field(
        min_length=1,
        description="JWT refresh token issued by /auth/device/token",
    )


class OnboardingStateRequest(RequestBase):
    """Request body for PUT /auth/onboarding."""

    step: Literal["verify", "path", "artifact", "apps", "done", "completed"] = Field(
        description='Current wizard step ("completed" ends the flow)',
        examples=["artifact"],
    )
    path: Literal["links", "api"] | None = Field(
        default=None,
        description="Chosen onboarding path, once the user has forked",
        examples=["links"],
    )
