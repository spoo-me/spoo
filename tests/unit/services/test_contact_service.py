"""Unit tests for Phase 9 — ContactService.

Embed formatting lives in DiscordOpsNotifier (tested in
tests/unit/infrastructure/test_ops_notify.py); these tests cover the
service's own concerns: gate order, failure policy, and what facts it
hands the notifier.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.errors import AppError, ForbiddenError, ValidationError

# ── Helpers ───────────────────────────────────────────────────────────────────


def make_service(captcha_ok=True, contact_sent=True, report_sent=True):
    from app.services.contact_service import ContactService

    notifier = AsyncMock()
    captcha = AsyncMock()

    captcha.verify = AsyncMock(return_value=captcha_ok)
    notifier.contact_message = AsyncMock(return_value=contact_sent)
    notifier.url_report = AsyncMock(return_value=report_sent)

    svc = ContactService(
        notifier=notifier,
        captcha=captcha,
    )
    return svc, notifier, captcha


# ── Tests: send_contact_message ───────────────────────────────────────────────


class TestSendContactMessage:
    @pytest.mark.asyncio
    async def test_success_does_not_raise(self):
        svc, _, _ = make_service(captcha_ok=True, contact_sent=True)
        await svc.send_contact_message(
            email="user@example.com",
            message="Hello",
            captcha_token="valid-token",
        )

    @pytest.mark.asyncio
    async def test_captcha_failure_raises_forbidden(self):
        svc, _, _ = make_service(captcha_ok=False)

        with pytest.raises(ForbiddenError, match="Invalid captcha"):
            await svc.send_contact_message(
                email="user@example.com",
                message="Hello",
                captcha_token="bad-token",
            )

    @pytest.mark.asyncio
    async def test_notify_failure_raises_app_error(self):
        svc, _, _ = make_service(captcha_ok=True, contact_sent=False)

        with pytest.raises(AppError, match="Error sending message"):
            await svc.send_contact_message(
                email="user@example.com",
                message="Hello",
                captcha_token="valid-token",
            )

    @pytest.mark.asyncio
    async def test_captcha_verified_before_notify(self):
        """Captcha must be checked before the notifier is called."""
        svc, notifier, _captcha = make_service(captcha_ok=False)

        with pytest.raises(ForbiddenError):
            await svc.send_contact_message(
                email="user@example.com",
                message="Hello",
                captcha_token="bad-token",
            )

        notifier.contact_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_notifier_receives_email_and_message(self):
        svc, notifier, _ = make_service()
        await svc.send_contact_message(
            email="user@example.com",
            message="Test message",
            captcha_token="valid-token",
        )
        notifier.contact_message.assert_awaited_once_with(
            "user@example.com", "Test message"
        )

    @pytest.mark.asyncio
    async def test_report_channel_not_used_for_contact(self):
        svc, notifier, _ = make_service()
        await svc.send_contact_message(
            email="user@example.com",
            message="Hello",
            captcha_token="valid-token",
        )
        notifier.url_report.assert_not_awaited()


# ── Tests: send_report ────────────────────────────────────────────────────────


class TestSendReport:
    @pytest.mark.asyncio
    async def test_success_does_not_raise(self):
        svc, _, _ = make_service(captcha_ok=True, report_sent=True)
        await svc.send_report(
            short_code="abc123",
            reason="spam",
            ip_address="1.2.3.4",
            app_url="https://spoo.me/",
            captcha_token="valid-token",
            url_exists=True,
        )

    @pytest.mark.asyncio
    async def test_captcha_failure_raises_forbidden(self):
        svc, _, _ = make_service(captcha_ok=False)

        with pytest.raises(ForbiddenError, match="Invalid captcha"):
            await svc.send_report(
                short_code="abc123",
                reason="spam",
                ip_address="1.2.3.4",
                app_url="https://spoo.me/",
                captcha_token="bad-token",
                url_exists=True,
            )

    @pytest.mark.asyncio
    async def test_url_not_found_raises_validation_error(self):
        svc, _, _ = make_service(captcha_ok=True)

        with pytest.raises(ValidationError, match="Invalid short code"):
            await svc.send_report(
                short_code="ghost",
                reason="spam",
                ip_address="1.2.3.4",
                app_url="https://spoo.me/",
                captcha_token="valid-token",
                url_exists=False,
            )

    @pytest.mark.asyncio
    async def test_notify_failure_raises_app_error(self):
        svc, _, _ = make_service(captcha_ok=True, report_sent=False)

        with pytest.raises(AppError, match="Error sending report"):
            await svc.send_report(
                short_code="abc123",
                reason="spam",
                ip_address="1.2.3.4",
                app_url="https://spoo.me/",
                captcha_token="valid-token",
                url_exists=True,
            )

    @pytest.mark.asyncio
    async def test_captcha_checked_before_existence(self):
        """Captcha must fail fast before the url_exists check matters."""
        svc, notifier, _captcha = make_service(captcha_ok=False)

        with pytest.raises(ForbiddenError):
            await svc.send_report(
                short_code="abc123",
                reason="spam",
                ip_address="1.2.3.4",
                app_url="https://spoo.me/",
                captcha_token="bad-token",
                url_exists=True,
            )

        notifier.url_report.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_notifier_receives_report_facts(self):
        svc, notifier, _ = make_service()
        await svc.send_report(
            short_code="abc123",
            reason="phishing",
            ip_address="1.2.3.4",
            app_url="https://spoo.me/",
            captcha_token="valid-token",
            url_exists=True,
        )
        notifier.url_report.assert_awaited_once_with(
            "abc123", "phishing", "1.2.3.4", "https://spoo.me/"
        )

    @pytest.mark.asyncio
    async def test_contact_channel_not_used_for_report(self):
        svc, notifier, _ = make_service()
        await svc.send_report(
            short_code="abc123",
            reason="spam",
            ip_address="1.2.3.4",
            app_url="https://spoo.me/",
            captcha_token="valid-token",
            url_exists=True,
        )
        notifier.contact_message.assert_not_awaited()
