"""
Response DTOs for authentication endpoints.

AuthProviderInfo    — auth provider entry in UserProfileResponse
UserPfp             — profile picture in UserProfileResponse
UserProfileResponse — shape returned by UserProfileResponse.from_user()
LoginResponse       — POST /auth/login  (200)
RegisterResponse    — POST /auth/register  (201)
RefreshResponse     — POST /auth/refresh  (200)
LogoutResponse      — POST /auth/logout  (200)
VerifyEmailResponse — POST /auth/verify-email  (200)
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import Field

from schemas.dto.base import ResponseBase
from schemas.models.user import OAuthProvider, ProviderProfile

if TYPE_CHECKING:
    from schemas.models.user import UserDoc


class AuthProviderInfo(ResponseBase):
    """Minimal OAuth provider entry returned inside UserProfileResponse."""

    provider: OAuthProvider | None = Field(
        default=None, description="OAuth provider name", examples=["google"]
    )
    email: str | None = Field(
        default=None,
        description="Email address from the OAuth provider",
        examples=["user@gmail.com"],
    )
    linked_at: datetime | None = Field(
        default=None,
        description="When the provider was linked",
        examples=["2025-01-15T10:30:00+00:00"],
    )


class UserPfp(ResponseBase):
    """Profile picture info returned inside UserProfileResponse."""

    url: str | None = Field(
        default=None,
        description="Profile picture URL",
        examples=["https://lh3.googleusercontent.com/a/photo"],
    )
    source: OAuthProvider | None = Field(
        default=None, description="Source of the profile picture", examples=["google"]
    )


class UserProfileResponse(ResponseBase):
    """User profile shape — used in login/register/me responses."""

    id: str = Field(description="User ID", examples=["507f1f77bcf86cd799439011"])
    email: str | None = Field(
        default=None, description="User's email address", examples=["user@example.com"]
    )
    email_verified: bool = Field(
        description="Whether the email address has been verified"
    )
    user_name: str | None = Field(
        default=None, description="Display name", examples=["Jane Doe"]
    )
    plan: str = Field(description="Subscription plan", examples=["free"])
    password_set: bool = Field(description="Whether the user has set a password")
    auth_providers: list[AuthProviderInfo] = Field(description="Linked OAuth providers")
    # pfp is absent from the JSON when None (route handlers use exclude_none=True)
    pfp: UserPfp | None = Field(
        default=None, description="Profile picture (absent when not set)"
    )

    @classmethod
    def from_user(cls, user: UserDoc) -> UserProfileResponse:
        """Build a UserProfileResponse from a UserDoc.

        This is the single authoritative place for the profile response shape,
        replacing the old AuthService.get_user_profile() static helper.
        """
        return cls(
            id=str(user.id),
            email=user.email,
            email_verified=user.email_verified,
            user_name=user.user_name,
            plan=user.plan,
            password_set=user.password_set,
            auth_providers=[
                AuthProviderInfo(
                    provider=p.provider,
                    email=p.email,
                    linked_at=p.linked_at,
                )
                for p in user.auth_providers
            ],
            pfp=UserPfp(url=user.pfp.url, source=user.pfp.source) if user.pfp else None,
        )


class LoginResponse(ResponseBase):
    """Response body for POST /auth/login (200)."""

    access_token: str = Field(
        description="JWT access token", examples=["eyJhbGciOiJIUzI1NiIs..."]
    )
    user: UserProfileResponse = Field(description="Authenticated user's profile")


class RegisterResponse(ResponseBase):
    """Response body for POST /auth/register (201)."""

    access_token: str = Field(
        description="JWT access token", examples=["eyJhbGciOiJIUzI1NiIs..."]
    )
    user: UserProfileResponse = Field(description="Newly created user's profile")
    requires_verification: bool = Field(
        description="Whether email verification is required before accessing protected resources"
    )
    verification_sent: bool = Field(
        description="Whether the verification email was sent successfully"
    )


class RefreshResponse(ResponseBase):
    """Response body for POST /auth/refresh (200)."""

    access_token: str = Field(
        description="New JWT access token", examples=["eyJhbGciOiJIUzI1NiIs..."]
    )


class LogoutResponse(ResponseBase):
    """Response body for POST /auth/logout (200)."""

    success: bool = Field(description="Always true on successful logout")


class VerifyEmailResponse(ResponseBase):
    """Response body for POST /auth/verify-email (200)."""

    success: bool = Field(description="Whether verification succeeded")
    message: str = Field(
        description="Human-readable status message",
        examples=["email verified successfully"],
    )
    email_verified: bool = Field(
        description="Updated email verification status (always true on success)"
    )


class MeResponse(ResponseBase):
    """Response body for GET /auth/me (200)."""

    user: UserProfileResponse = Field(
        description="Current authenticated user's profile"
    )


class SendVerificationResponse(ResponseBase):
    """Response body for POST /auth/send-verification (200)."""

    success: bool = Field(description="Whether the verification email was sent")
    message: str = Field(description="Human-readable status message")
    expires_in: int = Field(
        description="OTP expiry duration in seconds", examples=[600]
    )


class DeviceTokenResponse(ResponseBase):
    """Response body for POST /auth/device/token (200)."""

    access_token: str = Field(description="JWT access token")
    refresh_token: str = Field(description="JWT refresh token")
    user: UserProfileResponse = Field(description="User profile")


class DeviceRefreshResponse(ResponseBase):
    """Response body for POST /auth/device/refresh (200)."""

    access_token: str = Field(description="New JWT access token")
    refresh_token: str = Field(description="New JWT refresh token")


class OAuthProviderDetail(ResponseBase):
    """Detailed OAuth provider entry for the providers list endpoint."""

    provider: OAuthProvider = Field(description="Provider name", examples=["google"])
    email: str | None = Field(default=None, description="Email from provider")
    email_verified: bool = Field(
        default=False, description="Email verified by provider"
    )
    linked_at: datetime | None = Field(
        default=None, description="When the provider was linked"
    )
    profile: ProviderProfile = Field(
        default_factory=ProviderProfile, description="Provider profile (name, picture)"
    )


class OAuthProvidersResponse(ResponseBase):
    """Response body for GET /oauth/providers (200)."""

    providers: list[OAuthProviderDetail] = Field(
        description="List of linked OAuth providers with name, email, and linked_at"
    )
    password_set: bool = Field(
        description="Whether the user has a password set (affects unlink eligibility)"
    )


class OnboardingStateResponse(ResponseBase):
    """Response body for GET/PUT /auth/onboarding (200).

    Empty state (all-null, completed=false) means the user has no stored
    progress — either onboarding never started or the 24h cache expired.
    """

    step: str | None = Field(
        default=None, description="Stored wizard step", examples=["artifact"]
    )
    path: str | None = Field(
        default=None, description="Chosen path (links or api)", examples=["links"]
    )
    completed: bool = Field(
        default=False, description="Whether the wizard was finished"
    )
