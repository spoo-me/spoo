"""Unit tests for auth request and response DTOs."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.dto.requests.auth import (
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
    VerifyEmailRequest,
)
from app.schemas.dto.responses.auth import (
    AuthProviderInfo,
    OAuthProviderDetail,
    OnboardingCompleteResponse,
    UserProfileResponse,
)

# ── Auth request DTOs ──────────────────────────────────────────────────────────


class TestLoginRequest:
    def test_valid(self):
        req = LoginRequest.model_validate(
            {"email": "u@example.com", "password": "Pass123!x"}
        )
        assert req.email == "u@example.com"

    @pytest.mark.parametrize(
        "payload",
        [{"password": "Pass123!x"}, {"email": "u@example.com"}, {}],
        ids=["missing_email", "missing_password", "missing_both"],
    )
    def test_missing_required_fields_rejected(self, payload):
        with pytest.raises(ValidationError):
            LoginRequest.model_validate(payload)


class TestRegisterRequest:
    def test_valid_minimal(self):
        req = RegisterRequest.model_validate(
            {"email": "u@example.com", "password": "Pass123!x"}
        )
        assert req.user_name is None

    def test_optional_user_name(self):
        req = RegisterRequest.model_validate(
            {"email": "u@example.com", "password": "Pass123!x", "user_name": "Alice"}
        )
        assert req.user_name == "Alice"


class TestVerifyEmailRequest:
    def test_valid(self):
        assert VerifyEmailRequest.model_validate({"code": "123456"}).code == "123456"

    def test_requires_code(self):
        with pytest.raises(ValidationError):
            VerifyEmailRequest.model_validate({})


class TestResetPasswordRequest:
    def test_valid(self):
        req = ResetPasswordRequest.model_validate(
            {"email": "u@example.com", "code": "123456", "password": "NewPass1!"}
        )
        assert req.code == "123456"

    def test_all_fields_required(self):
        with pytest.raises(ValidationError):
            ResetPasswordRequest.model_validate({"email": "u@example.com"})


# ── Auth response DTOs ─────────────────────────────────────────────────────────


class TestUserProfileResponse:
    def test_minimal(self):
        r = UserProfileResponse(
            id="507f1f77bcf86cd799439011",
            email="u@example.com",
            email_verified=False,
            plan="free",
            password_set=False,
            auth_providers=[],
        )
        d = r.model_dump()
        assert d["id"] == "507f1f77bcf86cd799439011"
        assert d["email_verified"] is False

    def test_pfp_none_excluded(self):
        r = UserProfileResponse(
            id="507f1f77bcf86cd799439011",
            email="u@example.com",
            email_verified=True,
            plan="free",
            password_set=True,
            auth_providers=[],
            pfp=None,
        )
        assert "pfp" not in r.model_dump(exclude_none=True)

    def test_with_auth_provider(self):
        r = UserProfileResponse(
            id="507f1f77bcf86cd799439011",
            email="u@example.com",
            email_verified=True,
            plan="free",
            password_set=False,
            auth_providers=[
                AuthProviderInfo(
                    provider="google",
                    email="u@example.com",
                    linked_at="2024-01-01T00:00:00Z",
                )
            ],
        )
        assert r.auth_providers[0].provider == "google"


# ── UTC stamping on the wire ───────────────────────────────────────────────────
#
# PyMongo (no tz_aware) hands back naive datetimes. Without an explicit
# offset in the JSON, `new Date("2026-07-13T09:00:00")` in a browser parses
# the value as LOCAL time and silently shifts the instant. These tests pin
# that every auth timestamp leaves with an explicit UTC offset.

_NAIVE = datetime(2025, 1, 15, 10, 30)  # what a Mongo read-back looks like
_AWARE = datetime(2025, 1, 15, 10, 30, tzinfo=timezone.utc)
_WIRE = "2025-01-15T10:30:00+00:00"


def _profile(**overrides) -> UserProfileResponse:
    base = {
        "id": "507f1f77bcf86cd799439011",
        "email": "u@example.com",
        "email_verified": True,
        "plan": "free",
        "password_set": True,
        "auth_providers": [],
    }
    base.update(overrides)
    return UserProfileResponse(**base)


class TestAuthTimestampsCarryUtcOffset:
    def test_onboarded_at_naive_is_stamped_utc(self):
        d = _profile(onboarded_at=_NAIVE).model_dump()
        assert d["onboarded_at"] == _WIRE

    def test_onboarded_at_aware_keeps_instant(self):
        d = _profile(onboarded_at=_AWARE).model_dump()
        assert d["onboarded_at"] == _WIRE

    def test_onboarded_at_none_stays_null(self):
        d = _profile().model_dump()
        assert d["onboarded_at"] is None

    def test_provider_linked_at_naive_is_stamped_utc(self):
        info = AuthProviderInfo(provider="google", linked_at=_NAIVE)
        assert info.model_dump()["linked_at"] == _WIRE

    def test_provider_linked_at_none_stays_null(self):
        info = AuthProviderInfo(provider="google")
        assert info.model_dump()["linked_at"] is None

    def test_oauth_provider_detail_linked_at_naive_is_stamped_utc(self):
        detail = OAuthProviderDetail(provider="google", linked_at=_NAIVE)
        assert detail.model_dump()["linked_at"] == _WIRE

    def test_onboarding_complete_naive_and_aware_match_exactly(self):
        # First call stamps an aware datetime, the idempotent repeat echoes
        # the naive Mongo read-back — the wire form must be identical.
        first = OnboardingCompleteResponse(success=True, onboarded_at=_AWARE)
        repeat = OnboardingCompleteResponse(success=True, onboarded_at=_NAIVE)
        assert (
            first.model_dump()["onboarded_at"]
            == repeat.model_dump()["onboarded_at"]
            == _WIRE
        )
