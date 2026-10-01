"""Unit tests for split auth services (CredentialService, EmailVerificationService, etc.)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import jwt as pyjwt
import pytest
from bson import ObjectId

from errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from schemas.models.user import UserDoc

# ── Constants ────────────────────────────────────────────────────────────────

USER_OID = ObjectId("aaaaaaaaaaaaaaaaaaaaaaaa")


# ── Helpers ──────────────────────────────────────────────────────────────────


def make_jwt_settings():
    from config import JWTSettings

    return JWTSettings(
        jwt_issuer="spoo.me",
        jwt_audience="spoo.me.api",
        access_token_ttl_seconds=900,
        refresh_token_ttl_seconds=2592000,
        jwt_secret="test-secret-key-at-least-32-chars!!",
        jwt_private_key="",
        jwt_public_key="",
    )


def make_user_doc(
    email_verified=True,
    password_set=False,
    password_hash=None,
    auth_providers=None,
    status="ACTIVE",
    user_name="Test User",
):
    doc = {
        "_id": USER_OID,
        "email": "test@example.com",
        "email_verified": email_verified,
        "password_hash": password_hash,
        "password_set": password_set,
        "user_name": user_name,
        "pfp": None,
        "auth_providers": auth_providers or [],
        "plan": "free",
        "signup_ip": "1.2.3.4",
        "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
        "status": status,
    }
    return UserDoc.from_mongo(doc)


def make_token_factory():
    from services.token_factory import TokenFactory

    return TokenFactory(make_jwt_settings())


def make_otp_service():
    from services.auth.otp import OtpService

    return OtpService(token_repo=AsyncMock())


def make_credential_service():
    from services.auth.credentials import CredentialService

    otp = make_otp_service()
    return CredentialService(
        user_repo=AsyncMock(),
        otp_service=otp,
        email=AsyncMock(),
        token_factory=make_token_factory(),
    )


def make_verification_service():
    from services.auth.verification import EmailVerificationService

    otp = make_otp_service()
    return EmailVerificationService(
        user_repo=AsyncMock(),
        otp_service=otp,
        email=AsyncMock(),
        token_factory=make_token_factory(),
    )


def make_password_service():
    from services.auth.password import PasswordService

    otp = make_otp_service()
    return PasswordService(
        user_repo=AsyncMock(),
        otp_service=otp,
        email=AsyncMock(),
    )


def make_device_auth_service(app_registry=None):
    from services.auth.device import DeviceAuthService

    return DeviceAuthService(
        user_repo=AsyncMock(),
        token_repo=AsyncMock(),
        token_factory=make_token_factory(),
        grant_repo=AsyncMock(),
        app_registry=app_registry,
    )


# ── JWT tests ─────────────────────────────────────────────────────────────────


class TestJWTHelpers:
    def test_generate_access_token_returns_string(self):
        tf = make_token_factory()
        token = tf.generate_access_token(make_user_doc(), amr="pwd")
        assert isinstance(token, str)

    def test_access_token_has_correct_claims(self):
        tf = make_token_factory()
        settings = make_jwt_settings()
        token = tf.generate_access_token(make_user_doc(email_verified=True), amr="pwd")
        payload = pyjwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
        )
        assert payload["sub"] == str(USER_OID)
        assert payload["iss"] == "spoo.me"
        assert payload["aud"] == "spoo.me.api"
        assert payload["amr"] == ["pwd"]
        assert payload["email"] == "test@example.com"
        assert payload["email_verified"] is True
        assert "type" not in payload
        assert payload["exp"] - payload["iat"] == 900

    def test_access_token_email_claim_is_lowercased(self):
        tf = make_token_factory()
        settings = make_jwt_settings()
        user = make_user_doc()
        user = user.model_copy(update={"email": "MixedCase@Example.COM"})
        token = tf.generate_access_token(user, amr="pwd")
        payload = pyjwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
        )
        assert payload["email"] == "mixedcase@example.com"

    def test_refresh_token_has_type_field(self):
        tf = make_token_factory()
        settings = make_jwt_settings()
        token = tf.generate_refresh_token(make_user_doc(), amr="google")
        payload = pyjwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
        )
        assert payload["type"] == "refresh"
        assert payload["amr"] == ["google"]
        assert payload["exp"] - payload["iat"] == 2592000

    def test_verify_token_returns_payload(self):
        tf = make_token_factory()
        token = tf.generate_access_token(make_user_doc(), amr="pwd")
        payload = tf.verify_token(token, token_type="access")
        assert payload["sub"] == str(USER_OID)

    def test_verify_refresh_token_requires_type_field(self):
        tf = make_token_factory()
        refresh = tf.generate_refresh_token(make_user_doc(), amr="pwd")
        access = tf.generate_access_token(make_user_doc(), amr="pwd")
        # refresh passes as "refresh"
        assert tf.verify_token(refresh, token_type="refresh")["type"] == "refresh"
        # access token fails as "refresh" (no type field)
        with pytest.raises(AuthenticationError):
            tf.verify_token(access, token_type="refresh")

    def test_verify_invalid_token_raises_auth_error(self):
        tf = make_token_factory()
        with pytest.raises(AuthenticationError):
            tf.verify_token("not.a.valid.token", token_type="access")

    @pytest.mark.asyncio
    async def test_issue_tokens_returns_valid_pair(self):
        tf = make_token_factory()
        settings = make_jwt_settings()
        user = make_user_doc()
        access, refresh = await tf.issue_tokens(user, "google")
        # Access token has no type field
        access_payload = pyjwt.decode(
            access,
            settings.jwt_secret,
            algorithms=["HS256"],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
        )
        assert access_payload["amr"] == ["google"]
        assert "type" not in access_payload
        # Refresh token has type=refresh
        refresh_payload = pyjwt.decode(
            refresh,
            settings.jwt_secret,
            algorithms=["HS256"],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
        )
        assert refresh_payload["type"] == "refresh"
        assert refresh_payload["amr"] == ["google"]


# ── Login tests ───────────────────────────────────────────────────────────────


class TestLogin:
    @pytest.mark.asyncio
    async def test_login_success(self):
        from infrastructure.crypto import hash_password

        svc = make_credential_service()
        hashed = hash_password("ValidPass1!")
        user = make_user_doc(
            password_hash=hashed, password_set=True, email_verified=True
        )
        svc._user_repo.find_by_email.return_value = user

        result = await svc.login("test@example.com", "ValidPass1!")
        assert result.user is user
        assert isinstance(result.access_token, str)
        assert isinstance(result.refresh_token, str)

    @pytest.mark.asyncio
    async def test_login_user_not_found_raises(self):
        svc = make_credential_service()
        svc._user_repo.find_by_email.return_value = None

        with pytest.raises(AuthenticationError, match="invalid credentials"):
            await svc.login("nouser@example.com", "anypass")

    @pytest.mark.asyncio
    async def test_login_no_password_hash_raises(self):
        svc = make_credential_service()
        user = make_user_doc(password_hash=None)
        svc._user_repo.find_by_email.return_value = user

        with pytest.raises(AuthenticationError, match="invalid credentials"):
            await svc.login("test@example.com", "anypass")

    @pytest.mark.asyncio
    async def test_login_wrong_password_raises(self):
        from infrastructure.crypto import hash_password

        svc = make_credential_service()
        hashed = hash_password("CorrectPass1!")
        user = make_user_doc(password_hash=hashed, password_set=True)
        svc._user_repo.find_by_email.return_value = user

        with pytest.raises(AuthenticationError, match="invalid credentials"):
            await svc.login("test@example.com", "WrongPass1!")

    @pytest.mark.asyncio
    async def test_login_blocked_when_pending_deletion(self):
        from errors import AccountPendingDeletionError
        from infrastructure.crypto import hash_password

        svc = make_credential_service()
        hashed = hash_password("ValidPass1!")
        user = make_user_doc(
            password_hash=hashed, password_set=True, status="PENDING_DELETION"
        )
        svc._user_repo.find_by_email.return_value = user

        with pytest.raises(AccountPendingDeletionError) as exc_info:
            await svc.login("test@example.com", "ValidPass1!")
        assert exc_info.value.status_code == 403
        assert exc_info.value.error_code == "ACCOUNT_PENDING_DELETION"

    @pytest.mark.asyncio
    async def test_login_blocked_when_erasing(self):
        """ERASING (cascade claimed) answers exactly like PENDING_DELETION —
        a fresh session must never outlive the erasure."""
        from errors import AccountPendingDeletionError
        from infrastructure.crypto import hash_password

        svc = make_credential_service()
        hashed = hash_password("ValidPass1!")
        user = make_user_doc(password_hash=hashed, password_set=True, status="ERASING")
        svc._user_repo.find_by_email.return_value = user

        with pytest.raises(AccountPendingDeletionError) as exc_info:
            await svc.login("test@example.com", "ValidPass1!")
        assert exc_info.value.error_code == "ACCOUNT_PENDING_DELETION"

    @pytest.mark.asyncio
    async def test_login_pending_deletion_hidden_behind_password_proof(self):
        """Wrong password on a pending account answers like any wrong
        password — strangers can't probe deletion state."""
        from infrastructure.crypto import hash_password

        svc = make_credential_service()
        hashed = hash_password("ValidPass1!")
        user = make_user_doc(
            password_hash=hashed, password_set=True, status="PENDING_DELETION"
        )
        svc._user_repo.find_by_email.return_value = user

        with pytest.raises(AuthenticationError, match="invalid credentials"):
            await svc.login("test@example.com", "WrongPass1!")

    @pytest.mark.asyncio
    async def test_login_invalid_creds_same_message_for_both_failures(self):
        """No user enumeration — both failure cases return identical message."""
        from infrastructure.crypto import hash_password

        svc = make_credential_service()

        svc._user_repo.find_by_email.return_value = None
        with pytest.raises(AuthenticationError) as exc_info:
            await svc.login("nobody@example.com", "pass")
        msg_no_user = str(exc_info.value)

        svc._user_repo.find_by_email.return_value = make_user_doc(
            password_hash=hash_password("CorrectPass1!"), password_set=True
        )
        with pytest.raises(AuthenticationError) as exc_info:
            await svc.login("test@example.com", "WrongPass1!")
        msg_wrong_pw = str(exc_info.value)

        assert msg_no_user == msg_wrong_pw


# ── Register tests ────────────────────────────────────────────────────────────


class TestRegister:
    @pytest.mark.asyncio
    async def test_register_success(self):
        svc = make_credential_service()
        svc._user_repo.find_by_email.return_value = None
        svc._user_repo.create.return_value = USER_OID
        svc._otp._token_repo.count_recent.return_value = 0
        svc._otp._token_repo.create.return_value = ObjectId()
        svc._email.send_verification_email.return_value = True

        result = await svc.register(
            "new@example.com", "ValidPass1!", "New User", "1.2.3.4"
        )
        assert result.user.email == "new@example.com"
        assert isinstance(result.access_token, str)
        assert isinstance(result.refresh_token, str)
        assert result.verification_sent is True

    @pytest.mark.asyncio
    async def test_register_duplicate_email_raises(self):
        svc = make_credential_service()
        svc._user_repo.find_by_email.return_value = make_user_doc()

        with pytest.raises(ConflictError, match="email already registered"):
            await svc.register("test@example.com", "ValidPass1!", None, None)

    @pytest.mark.asyncio
    async def test_register_weak_password_raises(self):
        svc = make_credential_service()
        svc._user_repo.find_by_email.return_value = None

        with pytest.raises(
            ValidationError, match="Password does not meet requirements"
        ):
            await svc.register("new@example.com", "weak", None, None)

    @pytest.mark.asyncio
    async def test_register_verification_email_failure_is_non_fatal(self):
        """Registration succeeds even if sending verification email fails."""
        svc = make_credential_service()
        svc._user_repo.find_by_email.return_value = None
        svc._user_repo.create.return_value = USER_OID
        svc._otp._token_repo.count_recent.return_value = 0
        svc._otp._token_repo.create.return_value = ObjectId()
        svc._email.send_verification_email.side_effect = Exception("email server down")

        result = await svc.register("new@example.com", "ValidPass1!", None, None)
        assert result.user is not None
        assert result.verification_sent is False

    @pytest.mark.asyncio
    async def test_register_race_condition_duplicate_raises(self):
        from pymongo.errors import DuplicateKeyError

        svc = make_credential_service()
        svc._user_repo.find_by_email.return_value = None
        svc._user_repo.create.side_effect = DuplicateKeyError("duplicate")

        with pytest.raises(ConflictError, match="email already registered"):
            await svc.register("new@example.com", "ValidPass1!", None, None)


# ── Refresh token tests ───────────────────────────────────────────────────────


class TestRefreshToken:
    @pytest.mark.asyncio
    async def test_refresh_token_success(self):
        svc = make_credential_service()
        user = make_user_doc()
        # Generate a valid refresh token via the token factory
        refresh_tok = svc._tokens.generate_refresh_token(user, amr="pwd")
        svc._user_repo.find_by_id.return_value = user

        result = await svc.refresh_token(refresh_tok)
        assert result.user is user
        assert isinstance(result.access_token, str)
        assert isinstance(result.refresh_token, str)
        assert result.app_id is None

    @pytest.mark.asyncio
    async def test_refresh_token_invalid_raises(self):
        svc = make_credential_service()

        with pytest.raises(AuthenticationError):
            await svc.refresh_token("invalid.token.here")

    @pytest.mark.asyncio
    async def test_refresh_token_user_not_found_raises(self):
        svc = make_credential_service()
        user = make_user_doc()
        refresh_tok = svc._tokens.generate_refresh_token(user, amr="pwd")
        svc._user_repo.find_by_id.return_value = None

        with pytest.raises(
            AuthenticationError, match="invalid or expired refresh token"
        ):
            await svc.refresh_token(refresh_tok)

    @pytest.mark.asyncio
    async def test_refresh_token_inactive_user_raises(self):
        svc = make_credential_service()
        user = make_user_doc()
        refresh_tok = svc._tokens.generate_refresh_token(user, amr="pwd")
        inactive_user = make_user_doc(status="INACTIVE")
        svc._user_repo.find_by_id.return_value = inactive_user

        with pytest.raises(AuthenticationError):
            await svc.refresh_token(refresh_tok)

    @pytest.mark.asyncio
    async def test_refresh_rejects_access_token(self):
        """An access token must not be accepted as a refresh token."""
        svc = make_credential_service()
        user = make_user_doc()
        access_tok = svc._tokens.generate_access_token(user, amr="pwd")

        with pytest.raises(AuthenticationError):
            await svc.refresh_token(access_tok)


# ── Verify email tests ────────────────────────────────────────────────────────


class TestVerifyEmail:
    def _make_token_doc(self, user_id, otp_code, expired=False, used=False, attempts=0):
        from infrastructure.crypto import hash_token
        from schemas.models.token import TOKEN_TYPE_EMAIL_VERIFY, VerificationTokenDoc

        now = datetime.now(timezone.utc)
        expires = now - timedelta(seconds=1) if expired else now + timedelta(minutes=10)
        doc = {
            "_id": ObjectId(),
            "user_id": user_id,
            "email": "test@example.com",
            "token_hash": hash_token(otp_code),
            "token_type": TOKEN_TYPE_EMAIL_VERIFY,
            "expires_at": expires,
            "created_at": now,
            "used_at": now if used else None,
            "attempts": attempts,
        }
        return VerificationTokenDoc.from_mongo(doc)

    @pytest.mark.asyncio
    async def test_verify_email_success(self):
        svc = make_verification_service()
        user = make_user_doc(email_verified=False)
        token_doc = self._make_token_doc(USER_OID, "123456")
        svc._user_repo.find_by_id.return_value = user
        svc._otp._token_repo.find_latest_by_user.return_value = token_doc
        svc._otp._token_repo.mark_as_used.return_value = True
        svc._user_repo.update.return_value = True
        svc._email.send_welcome_email.return_value = True

        access, refresh = await svc.verify_email(str(USER_OID), "123456")
        assert isinstance(access, str)
        assert isinstance(refresh, str)
        # New tokens must have email_verified=True
        settings = make_jwt_settings()
        payload = pyjwt.decode(
            access,
            settings.jwt_secret,
            algorithms=["HS256"],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
        )
        assert payload["email_verified"] is True

    @pytest.mark.asyncio
    async def test_verify_email_already_verified_raises(self):
        svc = make_verification_service()
        user = make_user_doc(email_verified=True)
        svc._user_repo.find_by_id.return_value = user

        with pytest.raises(ValidationError, match="email already verified"):
            await svc.verify_email(str(USER_OID), "123456")

    @pytest.mark.asyncio
    async def test_verify_email_invalid_otp_raises(self):
        svc = make_verification_service()
        user = make_user_doc(email_verified=False)
        svc._user_repo.find_by_id.return_value = user
        svc._otp._token_repo.find_latest_by_user.return_value = None  # token not found

        with pytest.raises(ValidationError):
            await svc.verify_email(str(USER_OID), "000000")

    @pytest.mark.asyncio
    async def test_verify_email_expired_otp_raises(self):
        svc = make_verification_service()
        user = make_user_doc(email_verified=False)
        token_doc = self._make_token_doc(USER_OID, "123456", expired=True)
        svc._user_repo.find_by_id.return_value = user
        svc._otp._token_repo.find_latest_by_user.return_value = token_doc

        with pytest.raises(ValidationError, match="expired"):
            await svc.verify_email(str(USER_OID), "123456")

    @pytest.mark.asyncio
    async def test_verify_email_max_attempts_raises(self):
        svc = make_verification_service()
        user = make_user_doc(email_verified=False)
        token_doc = self._make_token_doc(USER_OID, "123456", attempts=5)
        svc._user_repo.find_by_id.return_value = user
        svc._otp._token_repo.find_latest_by_user.return_value = token_doc

        with pytest.raises(ValidationError, match="Too many failed attempts"):
            await svc.verify_email(str(USER_OID), "123456")

    @pytest.mark.asyncio
    async def test_verify_email_wrong_code_increments_attempts(self):
        svc = make_verification_service()
        user = make_user_doc(email_verified=False)
        token_doc = self._make_token_doc(USER_OID, "123456")
        svc._user_repo.find_by_id.return_value = user
        svc._otp._token_repo.find_latest_by_user.return_value = token_doc
        svc._otp._token_repo.increment_attempts.return_value = True

        with pytest.raises(ValidationError, match="Invalid or expired"):
            await svc.verify_email(str(USER_OID), "999999")  # wrong code
        svc._otp._token_repo.increment_attempts.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_verify_email_welcome_email_failure_is_non_fatal(self):
        svc = make_verification_service()
        user = make_user_doc(email_verified=False)
        token_doc = self._make_token_doc(USER_OID, "123456")
        svc._user_repo.find_by_id.return_value = user
        svc._otp._token_repo.find_latest_by_user.return_value = token_doc
        svc._otp._token_repo.mark_as_used.return_value = True
        svc._user_repo.update.return_value = True
        svc._email.send_welcome_email.side_effect = Exception("server down")

        # Should NOT raise — welcome email is best-effort
        access, _refresh = await svc.verify_email(str(USER_OID), "123456")
        assert access is not None


# ── Send verification tests ──────────────────────────────────────────────────


class TestSendVerification:
    @pytest.mark.asyncio
    async def test_send_verification_success(self):
        svc = make_verification_service()
        svc._user_repo.find_by_id.return_value = make_user_doc(email_verified=False)
        svc._otp._token_repo.count_recent.return_value = 0
        svc._otp._token_repo.create.return_value = ObjectId()
        svc._email.send_verification_email.return_value = True

        await svc.send_verification(str(USER_OID))
        svc._email.send_verification_email.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_verification_already_verified_raises(self):
        svc = make_verification_service()
        svc._user_repo.find_by_id.return_value = make_user_doc(email_verified=True)

        with pytest.raises(ValidationError, match="already verified"):
            await svc.send_verification(str(USER_OID))

    @pytest.mark.asyncio
    async def test_send_verification_user_not_found_raises(self):
        svc = make_verification_service()
        svc._user_repo.find_by_id.return_value = None

        with pytest.raises(NotFoundError, match="user not found"):
            await svc.send_verification(str(USER_OID))

    @pytest.mark.asyncio
    async def test_send_verification_rate_limited_by_service(self):
        """Service-level rate limit: raises when MAX_TOKENS_PER_HOUR exceeded."""
        from errors import RateLimitError

        svc = make_verification_service()
        svc._user_repo.find_by_id.return_value = make_user_doc(email_verified=False)
        svc._otp._token_repo.count_recent.return_value = 3  # MAX_TOKENS_PER_HOUR

        with pytest.raises(RateLimitError, match="Too many verification attempts"):
            await svc.send_verification(str(USER_OID))

        # Email must NOT be sent when rate limited
        svc._email.send_verification_email.assert_not_called()


# ── OTP rate limit tests ─────────────────────────────────────────────────────


class TestOTPRateLimit:
    @pytest.mark.asyncio
    async def test_create_otp_at_limit_raises(self):
        """create_otp raises RateLimitError when count_recent >= MAX_TOKENS_PER_HOUR."""
        from errors import RateLimitError
        from schemas.models.token import TOKEN_TYPE_EMAIL_VERIFY

        svc = make_otp_service()
        svc._token_repo.count_recent.return_value = 3

        with pytest.raises(RateLimitError):
            await svc.create_otp(USER_OID, "test@example.com", TOKEN_TYPE_EMAIL_VERIFY)

        svc._token_repo.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_otp_below_limit_succeeds(self):
        from schemas.models.token import TOKEN_TYPE_EMAIL_VERIFY

        svc = make_otp_service()
        svc._token_repo.count_recent.return_value = 2
        svc._token_repo.create.return_value = ObjectId()

        otp = await svc.create_otp(
            USER_OID, "test@example.com", TOKEN_TYPE_EMAIL_VERIFY
        )
        assert isinstance(otp, str)
        assert len(otp) == 6

    @pytest.mark.asyncio
    async def test_create_otp_password_reset_rate_limit_message(self):
        """Password reset rate limit has a different error message."""
        from errors import RateLimitError
        from schemas.models.token import TOKEN_TYPE_PASSWORD_RESET

        svc = make_otp_service()
        svc._token_repo.count_recent.return_value = 3

        with pytest.raises(RateLimitError, match="password reset"):
            await svc.create_otp(
                USER_OID, "test@example.com", TOKEN_TYPE_PASSWORD_RESET
            )


# ── Request password reset tests ──────────────────────────────────────────────


class TestRequestPasswordReset:
    @pytest.mark.asyncio
    async def test_always_returns_none_for_nonexistent_email(self):
        """Timing-safe: nonexistent email returns normally."""
        svc = make_password_service()
        svc._user_repo.find_by_email.return_value = None

        result = await svc.request_password_reset("nobody@example.com")
        assert result is None

    @pytest.mark.asyncio
    async def test_always_returns_none_when_no_password_set(self):
        """Timing-safe: OAuth-only user returns normally."""
        svc = make_password_service()
        user = make_user_doc(password_set=False)
        svc._user_repo.find_by_email.return_value = user

        result = await svc.request_password_reset("test@example.com")
        assert result is None

    @pytest.mark.asyncio
    async def test_always_returns_none_when_rate_limited(self):
        """Timing-safe: rate limited returns normally."""
        svc = make_password_service()
        user = make_user_doc(password_set=True)
        svc._user_repo.find_by_email.return_value = user
        svc._otp._token_repo.count_recent.return_value = 99  # definitely rate limited

        result = await svc.request_password_reset("test@example.com")
        assert result is None

    @pytest.mark.asyncio
    async def test_sends_email_when_valid(self):
        svc = make_password_service()
        user = make_user_doc(password_set=True)
        svc._user_repo.find_by_email.return_value = user
        svc._otp._token_repo.count_recent.return_value = 0
        svc._otp._token_repo.delete_by_user.return_value = 0
        svc._otp._token_repo.create.return_value = ObjectId()
        svc._email.send_password_reset_email.return_value = True

        await svc.request_password_reset("test@example.com")
        svc._email.send_password_reset_email.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_always_returns_none_on_send_failure(self):
        """Timing-safe: email send failure returns normally."""
        svc = make_password_service()
        user = make_user_doc(password_set=True)
        svc._user_repo.find_by_email.return_value = user
        svc._otp._token_repo.count_recent.return_value = 0
        svc._otp._token_repo.delete_by_user.return_value = 0
        svc._otp._token_repo.create.return_value = ObjectId()
        svc._email.send_password_reset_email.return_value = False

        result = await svc.request_password_reset("test@example.com")
        assert result is None


# ── Reset password tests ──────────────────────────────────────────────────────


class TestResetPassword:
    def _make_token_doc(self, user_id, otp_code, expired=False):
        from infrastructure.crypto import hash_token
        from schemas.models.token import TOKEN_TYPE_PASSWORD_RESET, VerificationTokenDoc

        now = datetime.now(timezone.utc)
        expires = now - timedelta(seconds=1) if expired else now + timedelta(minutes=10)
        doc = {
            "_id": ObjectId(),
            "user_id": user_id,
            "email": "test@example.com",
            "token_hash": hash_token(otp_code),
            "token_type": TOKEN_TYPE_PASSWORD_RESET,
            "expires_at": expires,
            "created_at": now,
            "used_at": None,
            "attempts": 0,
        }
        return VerificationTokenDoc.from_mongo(doc)

    @pytest.mark.asyncio
    async def test_reset_password_success(self):
        svc = make_password_service()
        user = make_user_doc(password_set=True)
        token_doc = self._make_token_doc(USER_OID, "654321")
        svc._user_repo.find_by_email.return_value = user
        svc._otp._token_repo.find_latest_by_user.return_value = token_doc
        svc._otp._token_repo.mark_as_used.return_value = True
        svc._user_repo.update.return_value = True

        await svc.reset_password("test@example.com", "654321", "NewValidPass1!")
        svc._user_repo.update.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reset_password_user_not_found_raises(self):
        svc = make_password_service()
        svc._user_repo.find_by_email.return_value = None

        with pytest.raises(ValidationError, match="invalid email or code"):
            await svc.reset_password("nobody@example.com", "000000", "NewValidPass1!")

    @pytest.mark.asyncio
    async def test_reset_password_weak_password_raises(self):
        svc = make_password_service()
        user = make_user_doc(password_set=True)
        svc._user_repo.find_by_email.return_value = user

        with pytest.raises(
            ValidationError, match="password does not meet requirements"
        ):
            await svc.reset_password("test@example.com", "654321", "weak")

    @pytest.mark.asyncio
    async def test_reset_password_wrong_otp_raises(self):
        svc = make_password_service()
        user = make_user_doc(password_set=True)
        svc._user_repo.find_by_email.return_value = user
        svc._otp._token_repo.find_latest_by_user.return_value = None  # token not found

        with pytest.raises(ValidationError):
            await svc.reset_password("test@example.com", "wrong-code", "NewValidPass1!")

    @pytest.mark.asyncio
    async def test_reset_password_wrong_code_increments_attempts(self):
        svc = make_password_service()
        user = make_user_doc(password_set=True)
        token_doc = self._make_token_doc(USER_OID, "654321")
        svc._user_repo.find_by_email.return_value = user
        svc._otp._token_repo.find_latest_by_user.return_value = token_doc
        svc._otp._token_repo.increment_attempts.return_value = True

        with pytest.raises(ValidationError, match="invalid email or code"):
            await svc.reset_password("test@example.com", "000000", "NewValidPass1!")
        svc._otp._token_repo.increment_attempts.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_request_password_reset_deletes_old_tokens_before_creating(self):
        """_create_password_reset_otp deletes old reset tokens before inserting new one."""
        svc = make_password_service()
        user = make_user_doc(password_set=True)
        svc._user_repo.find_by_email.return_value = user
        svc._otp._token_repo.count_recent.return_value = 0
        svc._otp._token_repo.delete_by_user.return_value = 1
        svc._otp._token_repo.create.return_value = ObjectId()
        svc._email.send_password_reset_email.return_value = True

        await svc.request_password_reset("test@example.com")
        svc._otp._token_repo.delete_by_user.assert_awaited()


# ── Set password tests ────────────────────────────────────────────────────────


class TestSetPassword:
    @pytest.mark.asyncio
    async def test_set_password_success(self):
        svc = make_password_service()
        user = make_user_doc(password_set=False)
        svc._user_repo.find_by_id.return_value = user
        svc._user_repo.update.return_value = True

        await svc.set_password(str(USER_OID), "NewValidPass1!")
        svc._user_repo.update.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_set_password_already_set_raises(self):
        svc = make_password_service()
        user = make_user_doc(password_set=True)
        svc._user_repo.find_by_id.return_value = user

        with pytest.raises(ValidationError, match="password already set"):
            await svc.set_password(str(USER_OID), "NewValidPass1!")

    @pytest.mark.asyncio
    async def test_set_password_user_not_found_raises(self):
        svc = make_password_service()
        svc._user_repo.find_by_id.return_value = None

        with pytest.raises(NotFoundError):
            await svc.set_password(str(USER_OID), "NewValidPass1!")

    @pytest.mark.asyncio
    async def test_set_password_weak_password_raises(self):
        svc = make_password_service()
        user = make_user_doc(password_set=False)
        svc._user_repo.find_by_id.return_value = user

        with pytest.raises(
            ValidationError, match="Password does not meet requirements"
        ):
            await svc.set_password(str(USER_OID), "weak")


# ── UserProfileResponse.from_user tests ───────────────────────────────────────


class TestGetUserProfile:
    def test_basic_profile(self):
        from schemas.dto.responses.auth import UserProfileResponse

        user = make_user_doc()
        profile = UserProfileResponse.from_user(user, plan="pro")
        assert profile.id == str(USER_OID)
        assert profile.plan == "pro"
        assert profile.pro_onboarded_at is None
        assert profile.email == "test@example.com"
        assert profile.email_verified is True
        assert profile.user_name == "Test User"
        assert profile.password_set is False
        assert profile.auth_providers == []

    def test_profile_with_oauth_provider(self):
        from schemas.dto.responses.auth import UserProfileResponse

        now = datetime(2024, 6, 1, tzinfo=timezone.utc)
        user_doc = {
            "_id": USER_OID,
            "email": "test@example.com",
            "email_verified": True,
            "password_hash": None,
            "password_set": False,
            "user_name": "Test",
            "pfp": None,
            "auth_providers": [
                {
                    "provider": "google",
                    "provider_user_id": "google123",
                    "email": "test@gmail.com",
                    "email_verified": True,
                    "profile": {"name": "Test User", "picture": "https://pic.url"},
                    "linked_at": now,
                }
            ],
            "plan": "free",
            "status": "ACTIVE",
        }
        user = UserDoc.from_mongo(user_doc)
        profile = UserProfileResponse.from_user(user, plan="free")
        assert len(profile.auth_providers) == 1
        assert profile.auth_providers[0].provider == "google"
        assert profile.auth_providers[0].linked_at == now

    def test_profile_with_pfp(self):
        from schemas.dto.responses.auth import UserProfileResponse

        user_doc = {
            "_id": USER_OID,
            "email": "test@example.com",
            "email_verified": True,
            "password_hash": None,
            "password_set": False,
            "user_name": "Test",
            "pfp": {"url": "https://img.url", "source": "google"},
            "auth_providers": [],
            "plan": "free",
            "status": "ACTIVE",
        }
        user = UserDoc.from_mongo(user_doc)
        profile = UserProfileResponse.from_user(user, plan="free")
        assert profile.pfp.url == "https://img.url"
        assert profile.pfp.source == "google"


# ── Extension auth flow tests ────────────────────────────────────────────────


# PKCE pair used across device auth tests (verifier → S256 challenge).
PKCE_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
PKCE_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def _make_device_token_doc(
    raw_code: str,
    *,
    code_challenge: str | None = PKCE_CHALLENGE,
    app_id: str | None = None,
):
    from infrastructure.crypto import hash_token
    from schemas.models.token import TOKEN_TYPE_DEVICE_AUTH, VerificationTokenDoc

    now = datetime.now(timezone.utc)
    return VerificationTokenDoc.from_mongo(
        {
            "_id": ObjectId(),
            "user_id": USER_OID,
            "email": "test@example.com",
            "token_hash": hash_token(raw_code),
            "token_type": TOKEN_TYPE_DEVICE_AUTH,
            "expires_at": now + timedelta(minutes=5),
            "created_at": now,
            "used_at": None,
            "attempts": 0,
            "app_id": app_id,
            "code_challenge": code_challenge,
        }
    )


def _make_grant_doc(app_id: str = "spoo-cli", scopes=None):
    from schemas.models.app_grant import AppGrantDoc

    return AppGrantDoc.from_mongo(
        {
            "_id": ObjectId(),
            "user_id": USER_OID,
            "app_id": app_id,
            "granted_at": datetime.now(timezone.utc),
            "last_used_at": None,
            "revoked_at": None,
            "scopes": scopes,
        }
    )


class TestExtensionAuth:
    @pytest.mark.asyncio
    async def test_create_device_auth_code(self):
        svc = make_device_auth_service()
        svc._token_repo.delete_by_user.return_value = 0
        svc._token_repo.create.return_value = ObjectId()

        code = await svc.create_device_auth_code(
            USER_OID, "test@example.com", code_challenge=PKCE_CHALLENGE
        )
        assert isinstance(code, str)
        assert len(code) > 30  # secure token is long
        svc._token_repo.create.assert_awaited_once()
        stored = svc._token_repo.create.await_args.args[0]
        assert stored["code_challenge"] == PKCE_CHALLENGE

    @pytest.mark.asyncio
    async def test_exchange_device_code_success(self):
        svc = make_device_auth_service()
        raw_code = "test-code-123"
        svc._token_repo.consume_by_hash.return_value = _make_device_token_doc(raw_code)
        svc._user_repo.find_by_id.return_value = make_user_doc(email_verified=True)

        result = await svc.exchange_device_code(raw_code, PKCE_VERIFIER)
        assert isinstance(result.access_token, str)
        assert isinstance(result.refresh_token, str)
        assert result.app_id is None  # no app_id set on this token
        svc._token_repo.consume_by_hash.assert_awaited_once()
        # No app grant → no scp claim (unrestricted)
        claims = pyjwt.decode(
            result.access_token,
            "test-secret-key-at-least-32-chars!!",
            algorithms=["HS256"],
            audience="spoo.me.api",
        )
        assert "scp" not in claims
        assert "app_id" not in claims

    @pytest.mark.asyncio
    async def test_exchange_device_code_wrong_verifier(self):
        svc = make_device_auth_service()
        raw_code = "test-code-123"
        svc._token_repo.consume_by_hash.return_value = _make_device_token_doc(raw_code)

        with pytest.raises(AuthenticationError, match="invalid or expired"):
            await svc.exchange_device_code(raw_code, "wrong-verifier" + "x" * 30)

    @pytest.mark.asyncio
    async def test_exchange_device_code_without_stored_challenge_rejected(self):
        """Codes minted without a challenge (pre-PKCE) are dead on arrival."""
        svc = make_device_auth_service()
        raw_code = "test-code-123"
        svc._token_repo.consume_by_hash.return_value = _make_device_token_doc(
            raw_code, code_challenge=None
        )

        with pytest.raises(AuthenticationError, match="invalid or expired"):
            await svc.exchange_device_code(raw_code, PKCE_VERIFIER)

    @pytest.mark.asyncio
    async def test_exchange_device_code_invalid(self):
        svc = make_device_auth_service()
        svc._token_repo.consume_by_hash.return_value = None

        with pytest.raises(AuthenticationError, match="invalid or expired"):
            await svc.exchange_device_code("bad-code", PKCE_VERIFIER)

    @pytest.mark.asyncio
    async def test_exchange_device_code_expired(self):
        """Expired codes are filtered out by consume_by_hash (expires_at in query)."""
        svc = make_device_auth_service()
        svc._token_repo.consume_by_hash.return_value = None

        with pytest.raises(AuthenticationError, match="invalid or expired"):
            await svc.exchange_device_code("expired-code", PKCE_VERIFIER)

    @pytest.mark.asyncio
    async def test_exchange_mints_scoped_token_from_grant(self):
        svc = make_device_auth_service()
        raw_code = "test-code-123"
        svc._token_repo.consume_by_hash.return_value = _make_device_token_doc(
            raw_code, app_id="spoo-cli"
        )
        svc._user_repo.find_by_id.return_value = make_user_doc(email_verified=True)
        svc._grant_repo.find_active_grant.return_value = _make_grant_doc(
            scopes=["shorten:create", "urls:read"]
        )

        result = await svc.exchange_device_code(raw_code, PKCE_VERIFIER)
        claims = pyjwt.decode(
            result.access_token,
            "test-secret-key-at-least-32-chars!!",
            algorithms=["HS256"],
            audience="spoo.me.api",
        )
        assert claims["scp"] == ["shorten:create", "urls:read"]
        assert claims["app_id"] == "spoo-cli"
        # Refresh token carries app_id but never scp
        refresh_claims = pyjwt.decode(
            result.refresh_token,
            "test-secret-key-at-least-32-chars!!",
            algorithms=["HS256"],
            audience="spoo.me.api",
        )
        assert refresh_claims["app_id"] == "spoo-cli"
        assert "scp" not in refresh_claims
        svc._grant_repo.touch_last_used.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_exchange_revoked_grant_rejected(self):
        svc = make_device_auth_service()
        raw_code = "test-code-123"
        svc._token_repo.consume_by_hash.return_value = _make_device_token_doc(
            raw_code, app_id="spoo-cli"
        )
        svc._user_repo.find_by_id.return_value = make_user_doc(email_verified=True)
        svc._grant_repo.find_active_grant.return_value = None

        with pytest.raises(AuthenticationError, match="revoked"):
            await svc.exchange_device_code(raw_code, PKCE_VERIFIER)

    @pytest.mark.asyncio
    async def test_exchange_legacy_grant_falls_back_to_registry_scopes(self):
        from schemas.models.app import AppEntry, AppStatus, AppType

        registry = {
            "spoo-cli": AppEntry(
                name="Spoo CLI",
                description="Terminal tool",
                status=AppStatus.LIVE,
                type=AppType.DEVICE_AUTH,
                scopes=["shorten:create"],
            )
        }
        svc = make_device_auth_service(app_registry=registry)
        raw_code = "test-code-123"
        svc._token_repo.consume_by_hash.return_value = _make_device_token_doc(
            raw_code, app_id="spoo-cli"
        )
        svc._user_repo.find_by_id.return_value = make_user_doc(email_verified=True)
        svc._grant_repo.find_active_grant.return_value = _make_grant_doc(scopes=None)

        result = await svc.exchange_device_code(raw_code, PKCE_VERIFIER)
        claims = pyjwt.decode(
            result.access_token,
            "test-secret-key-at-least-32-chars!!",
            algorithms=["HS256"],
            audience="spoo.me.api",
        )
        assert claims["scp"] == ["shorten:create"]

    @pytest.mark.asyncio
    async def test_exchange_legacy_grant_without_registry_stays_unrestricted(self):
        svc = make_device_auth_service()  # empty registry — app vanished
        raw_code = "test-code-123"
        svc._token_repo.consume_by_hash.return_value = _make_device_token_doc(
            raw_code, app_id="spoo-gone"
        )
        svc._user_repo.find_by_id.return_value = make_user_doc(email_verified=True)
        svc._grant_repo.find_active_grant.return_value = _make_grant_doc(
            app_id="spoo-gone", scopes=None
        )

        result = await svc.exchange_device_code(raw_code, PKCE_VERIFIER)
        claims = pyjwt.decode(
            result.access_token,
            "test-secret-key-at-least-32-chars!!",
            algorithms=["HS256"],
            audience="spoo.me.api",
        )
        assert "scp" not in claims
        assert claims["app_id"] == "spoo-gone"


class TestDeviceRefresh:
    def _refresh_token_for(self, app_id=None):
        tf = make_token_factory()
        return tf.generate_refresh_token(make_user_doc(), amr="ext", app_id=app_id)

    @pytest.mark.asyncio
    async def test_refresh_reminted_scopes_follow_grant(self):
        """scp is re-read from the grant at every refresh — updates propagate."""
        svc = make_device_auth_service()
        svc._user_repo.find_by_id.return_value = make_user_doc(email_verified=True)
        svc._grant_repo.find_active_grant.return_value = _make_grant_doc(
            scopes=["urls:read"]
        )

        result = await svc.refresh_device_tokens(self._refresh_token_for("spoo-cli"))
        claims = pyjwt.decode(
            result.access_token,
            "test-secret-key-at-least-32-chars!!",
            algorithms=["HS256"],
            audience="spoo.me.api",
        )
        assert claims["scp"] == ["urls:read"]
        assert claims["app_id"] == "spoo-cli"

    @pytest.mark.asyncio
    async def test_refresh_revoked_grant_rejected(self):
        svc = make_device_auth_service()
        svc._user_repo.find_by_id.return_value = make_user_doc(email_verified=True)
        svc._grant_repo.find_active_grant.return_value = None

        with pytest.raises(AuthenticationError, match="revoked"):
            await svc.refresh_device_tokens(self._refresh_token_for("spoo-cli"))

    @pytest.mark.asyncio
    async def test_refresh_without_app_id_skips_grant_check(self):
        svc = make_device_auth_service()
        svc._user_repo.find_by_id.return_value = make_user_doc(email_verified=True)

        result = await svc.refresh_device_tokens(self._refresh_token_for(None))
        assert isinstance(result.access_token, str)
        svc._grant_repo.find_active_grant.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refresh_invalid_token_rejected(self):
        svc = make_device_auth_service()

        with pytest.raises(AuthenticationError):
            await svc.refresh_device_tokens("not-a-jwt")
