"""ZeptoMail implementation of EmailProvider.

Ported from utils/email_service.py:
- sync requests → async httpx via HttpClient
- module-level os.getenv → injected EmailSettings + app_url
- Jinja2 template rendering is unchanged
"""

import os
from datetime import datetime, timezone

from jinja2 import Environment, FileSystemLoader, select_autoescape

from config import EmailSettings
from infrastructure.http_client import HttpClient
from infrastructure.logging import get_logger

log = get_logger(__name__)

_ZEPTO_API_URL = "https://api.zeptomail.in/v1.1/email"
_DEFAULT_TEMPLATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "templates",
    "emails",
)


def _format_utc(dt: datetime) -> str:
    """Human-readable UTC timestamp, e.g. ``August 26, 2026 at 14:03 UTC``.

    Naive datetimes are treated as UTC — that is how Mongo hands them back.
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return f"{dt:%B} {dt.day}, {dt.year} at {dt:%H:%M} UTC"


class ZeptoMailProvider:
    def __init__(
        self,
        settings: EmailSettings,
        http_client: HttpClient,
        app_url: str = "https://spoo.me",
        template_dir: str = _DEFAULT_TEMPLATE_DIR,
    ) -> None:
        self._settings = settings
        self._http = http_client
        self._app_url = app_url
        self._jinja = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=select_autoescape(["html", "xml"]),
        )

    async def _send(
        self,
        to_email: str,
        to_name: str | None,
        subject: str,
        html_body: str,
        text_body: str | None = None,
    ) -> bool:
        if not self._settings.zepto_api_token:
            log.error("zepto_mail_send_failed", reason="token_not_configured")
            return False

        payload: dict = {
            "from": {
                "address": self._settings.zepto_from_email,
                "name": self._settings.zepto_from_name,
            },
            "to": [
                {
                    "email_address": {
                        "address": to_email,
                        "name": to_name or to_email,
                    }
                }
            ],
            "subject": subject,
            "htmlbody": html_body,
        }
        if text_body:
            payload["textbody"] = text_body

        token = self._settings.zepto_api_token
        if not token.startswith("Zoho-enczapikey "):
            token = f"Zoho-enczapikey {token}"

        headers = {"Authorization": token, "Content-Type": "application/json"}

        try:
            response = await self._http.post(
                _ZEPTO_API_URL, json=payload, headers=headers
            )
            if response.status_code in (200, 201, 202):
                log.info("email_sent_success", to_email=to_email, subject=subject)
                return True
            log.error(
                "email_sent_failed",
                to_email=to_email,
                subject=subject,
                status_code=response.status_code,
                response=response.text[:200],
            )
            return False
        except Exception as e:
            log.error(
                "email_send_error",
                to_email=to_email,
                subject=subject,
                error=str(e),
                error_type=type(e).__name__,
            )
            return False

    async def send_verification_email(
        self, email: str, user_name: str | None, otp_code: str
    ) -> bool:
        subject = "Verify your email - spoo.me"
        template = self._jinja.get_template("verification.html")
        html_body = template.render(
            otp_code=otp_code, user_name=user_name, app_url=self._app_url
        )
        text_body = (
            f"Verify Your Email - spoo.me\n\n"
            f"Hello{f' {user_name}' if user_name else ''},\n\n"
            f"Your verification code is: {otp_code}\n\n"
            f"This code expires in 10 minutes.\n\n"
            f"© 2025 spoo.me. All rights reserved."
        )
        return await self._send(email, user_name, subject, html_body, text_body)

    async def send_welcome_email(self, email: str, user_name: str | None) -> bool:
        subject = "Welcome to spoo.me! 🎉"
        template = self._jinja.get_template("welcome.html")
        html_body = template.render(user_name=user_name, app_url=self._app_url)
        text_body = (
            f"Welcome to spoo.me{f', {user_name}' if user_name else ''}!\n\n"
            f"Get started: {self._app_url}/dashboard\n\n"
            f"© 2025 spoo.me. All rights reserved."
        )
        return await self._send(email, user_name, subject, html_body, text_body)

    async def send_html(
        self,
        to_email: str,
        subject: str,
        template_name: str,
        context: dict,
        text_body: str,
    ) -> bool:
        """Render ``templates/emails/<template_name>`` with ``context`` and send it."""
        html_body = self._jinja.get_template(template_name).render(
            app_url=self._app_url, **context
        )
        return await self._send(to_email, None, subject, html_body, text_body)

    async def send_deletion_requested(
        self, email: str, purge_after: datetime, restore_token: str | None = None
    ) -> bool:
        subject = "Your spoo.me account is scheduled for deletion"
        purge_date = _format_utc(purge_after)
        # The one-shot cancel link — the only restore path for OAuth-only
        # accounts (no password to restore with at the login page).
        restore_url = (
            f"{self._app_url}/restore-account?token={restore_token}"
            if restore_token
            else None
        )
        template = self._jinja.get_template("deletion_requested.html")
        html_body = template.render(
            purge_date=purge_date, app_url=self._app_url, restore_url=restore_url
        )
        if restore_url:
            cancel_copy = (
                f"Changed your mind? You can cancel the deletion any time before\n"
                f"the date above by clicking this link:\n{restore_url}\n\n"
                f"If your account has a password, you can also cancel by logging\n"
                f"back in via the restore option at {self._app_url}/login\n\n"
            )
        else:
            cancel_copy = (
                f"Changed your mind? Signing in normally is blocked while the deletion\n"
                f"is pending, but you can cancel it any time before the date above:\n"
                f"log back in via the restore option at {self._app_url}/login\n\n"
            )
        text_body = (
            f"Account Scheduled for Deletion - spoo.me\n\n"
            f"Hello,\n\n"
            f"We received a request to permanently delete your spoo.me account.\n"
            f"The deletion is scheduled for: {purge_date}\n\n"
            f"Once that date passes, your account, your short links, and all of\n"
            f"their analytics data will be permanently erased. This cannot be undone.\n\n"
            f"{cancel_copy}"
            f"If you didn't request this, email support@spoo.me immediately.\n\n"
            f"Support Team, spoo.me\n\n"
            f"© 2026 spoo.me. All rights reserved."
        )
        return await self._send(email, None, subject, html_body, text_body)

    async def send_deletion_cancelled(self, email: str) -> bool:
        subject = "Your spoo.me account deletion was cancelled"
        template = self._jinja.get_template("deletion_cancelled.html")
        html_body = template.render(app_url=self._app_url)
        text_body = (
            "Account Deletion Cancelled - spoo.me\n\n"
            "Hello,\n\n"
            "The scheduled deletion of your spoo.me account has been cancelled.\n"
            "Your account is active again and nothing was deleted.\n\n"
            "If you did not cancel this deletion yourself, someone else may have\n"
            "access to your account or inbox. Change your password and email\n"
            "support@spoo.me immediately.\n\n"
            "Support Team, spoo.me\n\n"
            "© 2026 spoo.me. All rights reserved."
        )
        return await self._send(email, None, subject, html_body, text_body)

    async def send_erasure_confirmation(self, email: str) -> bool:
        subject = "Your spoo.me account has been deleted"
        template = self._jinja.get_template("deletion_completed.html")
        html_body = template.render(app_url=self._app_url)
        text_body = (
            "Account Deleted - spoo.me\n\n"
            "Hello,\n\n"
            "Your spoo.me account has been permanently deleted, as requested.\n"
            "Your account details, your short links, and all of their analytics\n"
            "data have been erased from our systems.\n\n"
            "Copies in our automated backups age out on their own within 15 days.\n"
            "There is nothing more you need to do.\n\n"
            "Thank you for using spoo.me. We're sorry to see you go, and you're\n"
            "welcome back any time.\n\n"
            "Support Team, spoo.me\n\n"
            "© 2026 spoo.me. All rights reserved."
        )
        return await self._send(email, None, subject, html_body, text_body)

    async def send_password_reset_email(
        self, email: str, user_name: str | None, otp_code: str
    ) -> bool:
        subject = "Reset your password - spoo.me"
        template = self._jinja.get_template("password_reset.html")
        html_body = template.render(
            otp_code=otp_code, user_name=user_name, app_url=self._app_url
        )
        text_body = (
            f"Reset Your Password - spoo.me\n\n"
            f"Hello{f' {user_name}' if user_name else ''},\n\n"
            f"Your password reset code is: {otp_code}\n\n"
            f"This code expires in 10 minutes.\n\n"
            f"© 2025 spoo.me. All rights reserved."
        )
        return await self._send(email, user_name, subject, html_body, text_body)
