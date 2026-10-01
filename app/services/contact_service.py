"""
ContactService — contact form and URL report handling.

Validates captcha via CaptchaProvider, then notifies the operator via
OpsNotifier.  Framework-agnostic: no FastAPI imports.

The route layer is responsible for:
    - Parsing form data (email, message, short_code, reason, captcha token)
    - Checking that the reported short_code exists (via UrlService)
    - HTTP response construction (redirect or render template)

The OpsNotifier owns all delivery formatting; this service owns the
gate order (captcha first) and the failure policy (a failed send is a
user-visible error — the notification IS the deliverable here).
"""

from __future__ import annotations

from app.errors import AppError, ForbiddenError, ValidationError
from app.infrastructure.captcha.protocol import CaptchaProvider
from app.infrastructure.logging import get_logger
from app.infrastructure.ops_notify import OpsNotifier

log = get_logger(__name__)


class ContactService:
    """Contact and report form service.

    Args:
        notifier: OpsNotifier that delivers to the operator's channels.
        captcha:  CaptchaProvider used to verify hCaptcha tokens.
    """

    def __init__(
        self,
        notifier: OpsNotifier,
        captcha: CaptchaProvider,
    ) -> None:
        self._notify = notifier
        self._captcha = captcha

    # ── Public API ────────────────────────────────────────────────────────────

    async def send_contact_message(
        self,
        email: str,
        message: str,
        captcha_token: str,
    ) -> None:
        """Send a contact form message to the operator.

        Args:
            email:         Sender's email address.
            message:       Message body.
            captcha_token: hCaptcha response token from the form.

        Raises:
            ForbiddenError: Captcha verification failed.
            AppError:       Notification send failed.
        """
        if not await self._captcha.verify(captcha_token):
            log.info("contact_captcha_failed")
            raise ForbiddenError("Invalid captcha, please try again")

        sent = await self._notify.contact_message(email, message)
        if not sent:
            log.error(
                "contact_notify_failed",
                email_domain=email.split("@")[1] if "@" in email else "unknown",
            )
            raise AppError("Error sending message, please try again later")

        log.info(
            "contact_message_sent",
            email_domain=email.split("@")[1] if "@" in email else "unknown",
            message_length=len(message),
        )

    async def send_report(
        self,
        short_code: str,
        reason: str,
        ip_address: str,
        app_url: str,
        captcha_token: str,
        url_exists: bool,
    ) -> None:
        """Send a URL report to the operator.

        Args:
            short_code:    The reported short code (already stripped to base code).
            reason:        Reporter's reason.
            ip_address:    Reporter's client IP.
            app_url:       Base URL of the application (e.g. ``"https://spoo.me/"``).
            captcha_token: hCaptcha response token.
            url_exists:    Whether the short_code was found in any URL collection.
                           The route layer performs the existence check.

        Raises:
            ForbiddenError:  Captcha verification failed.
            ValidationError: short_code does not exist.
            AppError:        Notification send failed.
        """
        if not await self._captcha.verify(captcha_token):
            log.info("report_captcha_failed", short_code=short_code)
            raise ForbiddenError("Invalid captcha, please try again")

        if not url_exists:
            raise ValidationError("Invalid short code, short code does not exist")

        sent = await self._notify.url_report(short_code, reason, ip_address, app_url)
        if not sent:
            log.error(
                "report_notify_failed",
                short_code=short_code,
            )
            raise AppError("Error sending report, please try again later")

        log.info("url_report_sent", short_code=short_code, reason=reason[:50])
