"""
PasswordService — set, request reset, and reset passwords.

Handles password management for both OAuth-only users (set initial password)
and password users (reset via OTP email flow).  All OTP operations are
delegated to OtpService.
"""

from __future__ import annotations

from datetime import datetime, timezone

from bson import ObjectId

from app.errors import AppError, NotFoundError, RateLimitError, ValidationError
from app.infrastructure.crypto import hash_password, hash_token
from app.infrastructure.email.protocol import EmailProvider
from app.infrastructure.logging import get_logger
from app.repositories.user_repository import UserRepository
from app.schemas.models.token import TOKEN_TYPE_PASSWORD_RESET
from app.services.auth.otp import OtpService
from app.shared.validators import validate_account_password

log = get_logger(__name__)


class PasswordService:
    """Password management: set, request reset, and reset.

    Args:
        user_repo:    Repository for the ``users`` collection.
        otp_service:  Shared OTP helper.
        email:        Email provider.
        account_password_min_length: Minimum password length.
        account_password_max_length: Maximum password length.
    """

    def __init__(
        self,
        user_repo: UserRepository,
        otp_service: OtpService,
        email: EmailProvider,
        account_password_min_length: int = 8,
        account_password_max_length: int = 128,
    ) -> None:
        self._user_repo = user_repo
        self._otp = otp_service
        self._email = email
        self._password_min_length = account_password_min_length
        self._password_max_length = account_password_max_length

    async def set_password(self, user_id: str, password: str) -> None:
        """Set a password for an OAuth-only user (no existing password).

        Raises:
            NotFoundError:   User not found.
            ValidationError: Password already set, or fails validation rules.
            AppError:        DB update failure.
        """
        svc_log = log.bind(op="auth.set_password")

        user_oid = ObjectId(user_id)
        user = await self._user_repo.find_by_id(user_oid)
        if not user:
            raise NotFoundError("user not found")

        if user.password_set:
            raise ValidationError("password already set")

        is_valid, missing, _ = validate_account_password(
            password,
            min_length=self._password_min_length,
            max_length=self._password_max_length,
        )
        if not is_valid:
            raise ValidationError(
                "Password does not meet requirements",
                details={"missing_requirements": missing},
            )

        new_hash = hash_password(password)
        updated = await self._user_repo.update(
            user_oid,
            {
                "$set": {
                    "password_hash": new_hash,
                    "password_set": True,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        if not updated:
            svc_log.error("password_set_failed", user_id=user_id, reason="no_update")
            raise AppError("failed to set password")

        svc_log.info("password_set", user_id=user_id)

    async def request_password_reset(self, email: str) -> None:
        """Request a password reset OTP.

        TIMING-SAFE: Always returns normally regardless of whether the email
        exists, has a password set, or is rate-limited. All errors are swallowed
        and logged. This prevents user enumeration.
        """
        svc_log = log.bind(op="auth.request_password_reset")

        try:
            user = await self._user_repo.find_by_email(email)
            if not user:
                svc_log.info("password_reset_requested_nonexistent")
                return

            if not user.password_set:
                svc_log.info("password_reset_no_password", user_id=str(user.id))
                return

            otp_code = await self._otp.create_otp(
                user.id,
                user.email,
                TOKEN_TYPE_PASSWORD_RESET,
                delete_existing=True,
            )

            email_sent = await self._email.send_password_reset_email(
                user.email, user.user_name, otp_code
            )
            if not email_sent:
                svc_log.error("password_reset_email_send_failed", user_id=str(user.id))
                return

            svc_log.info("password_reset_email_sent", user_id=str(user.id))

        except RateLimitError:
            svc_log.info("password_reset_rate_limited")
        except Exception as exc:
            svc_log.error(
                "password_reset_unexpected_error",
                error=str(exc),
                error_type=type(exc).__name__,
            )

    async def reset_password(
        self, email: str, otp_code: str, new_password: str
    ) -> None:
        """Reset password using an OTP code.

        Raises:
            ValidationError: User not found, OTP invalid, or password fails rules.
            AppError:        DB update failure.
        """
        svc_log = log.bind(op="auth.reset_password")

        user = await self._user_repo.find_by_email(email)

        is_valid, missing, _ = validate_account_password(
            new_password,
            min_length=self._password_min_length,
            max_length=self._password_max_length,
        )
        if not is_valid:
            raise ValidationError(
                "password does not meet requirements",
                details={"missing_requirements": missing},
            )

        if not user:
            # Simulate OTP verification timing to prevent user enumeration
            _dummy_hash = hash_token(otp_code)
            raise ValidationError("invalid email or code")

        try:
            await self._otp.verify_otp(user.id, otp_code, TOKEN_TYPE_PASSWORD_RESET)
        except ValidationError:
            # Re-raise with generic message to prevent user enumeration
            # via distinct error messages (verify_otp already logs the details)
            raise ValidationError("invalid email or code") from None

        new_hash = hash_password(new_password)
        updated = await self._user_repo.update(
            user.id,
            {
                "$set": {
                    "password_hash": new_hash,
                    "password_set": True,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        if not updated:
            svc_log.error("password_reset_update_failed", user_id=str(user.id))
            raise AppError("failed to reset password")

        svc_log.info("password_reset_success", user_id=str(user.id))
