"""Unit tests for ZeptoMailProvider."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from app.config import EmailSettings
from app.infrastructure.email.zeptomail import ZeptoMailProvider


class TestZeptoMailProvider:
    def _make(self, token="test-token"):
        settings = EmailSettings(
            zepto_api_token=token,
            zepto_from_email="noreply@spoo.me",
            zepto_from_name="spoo.me",
        )
        http = MagicMock()
        # Patch template rendering so tests don't need real template files
        jinja = MagicMock()
        jinja.get_template.return_value.render.return_value = "<html>test</html>"
        provider = ZeptoMailProvider(
            settings=settings, http_client=http, app_url="https://spoo.me"
        )
        provider._jinja = jinja
        return provider, http

    async def test_send_verification_makes_post(self):
        provider, http = self._make()
        resp = MagicMock(status_code=200)
        http.post = AsyncMock(return_value=resp)
        result = await provider.send_verification_email(
            "user@example.com", "Alice", "123456"
        )
        assert result is True
        http.post.assert_awaited_once()

    async def test_returns_false_when_token_empty(self):
        provider, _ = self._make(token="")
        assert (
            await provider.send_verification_email("u@e.com", None, "000000") is False
        )

    async def test_returns_false_on_non_2xx(self):
        provider, http = self._make()
        resp = MagicMock(status_code=422, text="Unprocessable")
        http.post = AsyncMock(return_value=resp)
        assert (
            await provider.send_verification_email("u@e.com", None, "000000") is False
        )

    async def test_returns_false_on_exception(self):
        provider, http = self._make()
        http.post = AsyncMock(side_effect=Exception("timeout"))
        assert (
            await provider.send_verification_email("u@e.com", None, "000000") is False
        )

    async def test_auth_header_prepends_prefix(self):
        provider, http = self._make(token="rawtoken")
        resp = MagicMock(status_code=200)
        http.post = AsyncMock(return_value=resp)
        await provider.send_welcome_email("u@e.com", "Alice")
        _, kwargs = http.post.call_args
        auth = kwargs["headers"]["Authorization"]
        assert auth == "Zoho-enczapikey rawtoken"

    async def test_auth_header_not_double_prefixed(self):
        provider, http = self._make(token="Zoho-enczapikey alreadyprefixed")
        resp = MagicMock(status_code=201)
        http.post = AsyncMock(return_value=resp)
        await provider.send_password_reset_email("u@e.com", None, "654321")
        _, kwargs = http.post.call_args
        auth = kwargs["headers"]["Authorization"]
        assert auth.count("Zoho-enczapikey") == 1


class TestDeletionEmails:
    """Account-deletion lifecycle mail — rendered against the REAL
    templates (no Jinja patching) so the copy assertions mean something."""

    PURGE_AFTER = datetime(2026, 8, 26, 14, 3, tzinfo=timezone.utc)

    def _make(self):
        settings = EmailSettings(
            zepto_api_token="test-token",
            zepto_from_email="noreply@spoo.me",
            zepto_from_name="spoo.me",
        )
        http = MagicMock()
        http.post = AsyncMock(return_value=MagicMock(status_code=200))
        provider = ZeptoMailProvider(
            settings=settings, http_client=http, app_url="https://spoo.me"
        )
        return provider, http

    def _sent_payload(self, http) -> dict:
        _, kwargs = http.post.call_args
        return kwargs["json"]

    async def test_deletion_requested_subject_and_copy(self):
        provider, http = self._make()
        result = await provider.send_deletion_requested(
            "user@example.com", self.PURGE_AFTER
        )
        assert result is True
        payload = self._sent_payload(http)
        assert payload["subject"] == "Your spoo.me account is scheduled for deletion"
        for body in (payload["htmlbody"], payload["textbody"]):
            assert "August 26, 2026 at 14:03 UTC" in body
            assert "support@spoo.me" in body
            assert "restore option" in body
            assert "Support Team, spoo.me" in body
        # Restore path points at the frontend login page.
        assert "https://spoo.me/login" in payload["htmlbody"]

    async def test_deletion_requested_with_token_carries_cancel_link(self):
        provider, http = self._make()
        result = await provider.send_deletion_requested(
            "user@example.com", self.PURGE_AFTER, restore_token="tok-secret-123"
        )
        assert result is True
        payload = self._sent_payload(http)
        cancel_url = "https://spoo.me/restore-account?token=tok-secret-123"
        for body in (payload["htmlbody"], payload["textbody"]):
            assert cancel_url in body
        # The password fallback stays documented next to the link.
        assert "https://spoo.me/login" in payload["textbody"]

    async def test_deletion_requested_naive_datetime_treated_as_utc(self):
        provider, http = self._make()
        await provider.send_deletion_requested(
            "user@example.com", datetime(2026, 8, 26, 14, 3)
        )
        assert "August 26, 2026 at 14:03 UTC" in self._sent_payload(http)["textbody"]

    async def test_deletion_completed_subject_and_copy(self):
        provider, http = self._make()
        result = await provider.send_erasure_confirmation("user@example.com")
        assert result is True
        payload = self._sent_payload(http)
        assert payload["subject"] == "Your spoo.me account has been deleted"
        for body in (payload["htmlbody"], payload["textbody"]):
            assert "permanently deleted" in body
            assert "15 days" in body
            assert "Support Team, spoo.me" in body

    async def test_deletion_cancelled_subject_and_copy(self):
        provider, http = self._make()
        result = await provider.send_deletion_cancelled("user@example.com")
        assert result is True
        payload = self._sent_payload(http)
        assert payload["subject"] == "Your spoo.me account deletion was cancelled"
        for body in (payload["htmlbody"], payload["textbody"]):
            assert "cancelled" in body
            # The unauthorized-restore warning \u2014 a silent restore would
            # hide an attacker cancelling a victim's deletion.
            assert "support@spoo.me" in body
            assert "Support Team, spoo.me" in body

    async def test_no_em_dash_in_any_email(self):
        provider, http = self._make()
        payloads = []
        await provider.send_deletion_requested(
            "user@example.com", self.PURGE_AFTER, restore_token="tok"
        )
        payloads.append(self._sent_payload(http))
        await provider.send_deletion_cancelled("user@example.com")
        payloads.append(self._sent_payload(http))
        await provider.send_erasure_confirmation("user@example.com")
        payloads.append(self._sent_payload(http))
        for payload in payloads:
            for part in (payload["subject"], payload["htmlbody"], payload["textbody"]):
                assert "\u2014" not in part  # em dash
                assert "\u2013" not in part  # en dash
