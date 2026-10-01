"""
EmailVerificationService — send and verify email OTPs.

Handles the post-registration email verification flow: sending OTPs,
verifying them, marking the user as verified, and issuing fresh tokens
with the updated ``email_verified`` claim.
"""

from __future__ import annotations

from datetime import datetime, timezone

from bson import ObjectId

from errors import AppError, NotFoundError, ValidationError
from infrastructure.email.protocol import EmailProvider
from infrastructure.logging import get_logger
from repositories.user_repository import UserRepository
from schemas.models.token import TOKEN_TYPE_EMAIL_VERIFY
from schemas.models.user import UserDoc
from services.auth.otp import OtpService
from services.token_factory import TokenFactory

log = get_logger(__name__)


class EmailVerificationService:
    """Email verification via OTP.

    Args:
        user_repo:     Repository for the ``users`` collection.
        otp_service:   Shared OTP helper.
        email:         Email provider.
        token_factory: JWT token generation.
    """

    def __init__(
        self,
        user_repo: UserRepository,
        otp_service: OtpService,
        email: EmailProvider,
        token_factory: TokenFactory,
    ) -> None:
        self._user_repo = user_repo
        self._otp = otp_service
        self._email = email
        self._tokens = token_factory

    async def verify_email(
        self, user_id: str, otp_code: str, *, amr: str = "pwd"
    ) -> tuple[str, str]:
        """Verify a user's email address using an OTP code.

        Marks the user's email as verified, sends a welcome email (best-effort),
        and issues new tokens with ``email_verified=True``.  The *amr* parameter
        preserves the original authentication method so OAuth users don't get
        tokens claiming password auth.

        Returns:
            (new_access_token, new_refresh_token)

        Raises:
            NotFoundError:   User not found.
            ValidationError: Email already verified, or OTP invalid/expired/used.
            AppError:        DB update failure.
        """
        svc_log = log.bind(op="auth.verify_email")

        user_oid = ObjectId(user_id)
        user = await self._user_repo.find_by_id(user_oid)
        if not user:
            raise NotFoundError("user not found")

        if user.email_verified:
            raise ValidationError("email already verified")

        await self._otp.verify_otp(user_oid, otp_code, TOKEN_TYPE_EMAIL_VERIFY)

        updated = await self._user_repo.update(
            user_oid,
            {
                "$set": {
                    "email_verified": True,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        if not updated:
            svc_log.error("email_verification_update_failed", user_id=user_id)
            raise AppError("failed to update verification status")

        svc_log.info("email_verified_success", user_id=user_id)

        # Build updated user doc for token generation
        user_data = user.model_dump(by_alias=True)
        user_data["_id"] = user_oid
        user_data["email_verified"] = True
        updated_user = UserDoc.model_validate(user_data)

        access_token, refresh_token = await self._tokens.issue_tokens(updated_user, amr)

        # Send welcome email — best-effort, do not fail verification on error
        try:
            await self._email.send_welcome_email(user.email, user.user_name)
        except Exception as exc:
            svc_log.error(
                "welcome_email_send_failed",
                user_id=user_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )

        return access_token, refresh_token

    async def send_verification(self, user_id: str) -> None:
        """Create and send a verification email OTP.

        Raises:
            NotFoundError:   User not found.
            ValidationError: Email already verified.
            RateLimitError:  Too many recent verification requests.
            AppError:        Failed to send email.
        """
        svc_log = log.bind(op="auth.send_verification")

        user_oid = ObjectId(user_id)
        user = await self._user_repo.find_by_id(user_oid)
        if not user:
            raise NotFoundError("user not found")

        if user.email_verified:
            raise ValidationError("email already verified")

        otp_code = await self._otp.create_otp(
            user_oid, user.email, TOKEN_TYPE_EMAIL_VERIFY
        )

        email_sent = await self._email.send_verification_email(
            user.email, user.user_name, otp_code
        )
        if not email_sent:
            svc_log.error("verification_email_send_failed", user_id=user_id)
            raise AppError("failed to send verification email")

        svc_log.info("verification_email_sent", user_id=user_id)
