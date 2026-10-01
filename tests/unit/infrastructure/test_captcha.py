"""Unit tests for HCaptchaProvider."""

from unittest.mock import AsyncMock, MagicMock

from app.infrastructure.captcha.hcaptcha import HCaptchaProvider


class TestHCaptchaProvider:
    def _make(self, secret="test-secret"):
        http = MagicMock()
        return HCaptchaProvider(secret=secret, http_client=http), http

    async def test_returns_true_on_success(self, mocker):
        provider, http = self._make()
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"success": True}
        http.post = AsyncMock(return_value=resp)
        assert await provider.verify("good-token") is True

    async def test_returns_false_on_failure(self, mocker):
        provider, http = self._make()
        resp = MagicMock(status_code=200)
        resp.json.return_value = {
            "success": False,
            "error-codes": ["invalid-input-response"],
        }
        http.post = AsyncMock(return_value=resp)
        assert await provider.verify("bad-token") is False

    async def test_skips_verification_when_secret_empty(self):
        provider, _ = self._make(secret="")
        assert await provider.verify("any-token") is True

    async def test_returns_false_on_http_error(self):
        provider, http = self._make()
        http.post = AsyncMock(side_effect=Exception("network error"))
        assert await provider.verify("token") is False

    async def test_returns_false_on_non_200_status(self):
        provider, http = self._make()
        resp = MagicMock(status_code=500, text="Internal Server Error")
        http.post = AsyncMock(return_value=resp)
        assert await provider.verify("token") is False
