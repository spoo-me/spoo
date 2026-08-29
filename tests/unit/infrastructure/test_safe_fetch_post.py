"""Direct tests for the webhook delivery POST layer (post_public) and the
address policy behind it — pinned here so a refactor cannot silently
regress SSRF behavior at the delivery layer."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from infrastructure.safe_fetch import (
    FetchHardError,
    FetchTransientError,
    PostResult,
    _is_public,
    _parse_retry_after,
    post_public,
)


class TestIsPublic:
    def test_public_v4(self):
        assert _is_public("93.184.216.34") is True

    @pytest.mark.parametrize(
        "ip",
        [
            "127.0.0.1",  # loopback
            "10.0.0.1",  # RFC1918
            "172.16.0.1",
            "192.168.1.1",
            "169.254.169.254",  # link-local / cloud metadata
            "100.64.0.1",  # CGNAT
            "0.0.0.0",
            "::1",  # v6 loopback
            "fc00::1",  # ULA
            "fe80::1",  # v6 link-local
            "::ffff:10.0.0.1",  # IPv4-mapped private
            "64:ff9b::7f00:1",  # NAT64-wrapped 127.0.0.1
            "64:ff9b::a00:1",  # NAT64-wrapped 10.0.0.1
            "ff02::1",  # multicast
        ],
    )
    def test_non_public_rejected(self, ip):
        assert _is_public(ip) is False

    def test_nat64_rejected_even_for_public_embedded(self):
        # The whole translation prefix is refused: a NAT64 literal is never
        # a legitimate webhook target, regardless of the embedded address.
        assert _is_public("64:ff9b::5db8:d822") is False


class TestPostPublic:
    @pytest.mark.asyncio
    async def test_http_rejected_without_any_network(self):
        result = await post_public("http://example.com/hook", "{}", headers={})
        assert result.status_code is None
        assert "https" in result.error

    @pytest.mark.asyncio
    async def test_private_resolution_is_an_outcome_not_an_exception(self):
        with patch(
            "infrastructure.safe_fetch.resolve_public_ip",
            AsyncMock(side_effect=FetchHardError("address is not public")),
        ):
            result = await post_public("https://internal.corp/hook", "{}", headers={})
        assert result.status_code is None
        assert "not public" in result.error

    @pytest.mark.asyncio
    async def test_transient_dns_failure_is_an_outcome_not_an_exception(self):
        """A DNS timeout must land in the retry ladder as a recorded
        attempt, never escape as an exception that skips bookkeeping."""
        with patch(
            "infrastructure.safe_fetch.resolve_public_ip",
            AsyncMock(side_effect=FetchTransientError("dns timeout")),
        ):
            result = await post_public("https://example.com/hook", "{}", headers={})
        assert result.status_code is None
        assert "dns timeout" in result.error

    @pytest.mark.asyncio
    async def test_redirects_are_refused(self):
        """Following a redirect would defeat IP pinning — 3xx is reported,
        never chased."""

        class _FakeResponse:
            status_code = 302
            headers = {"location": "https://10.0.0.1/"}  # noqa: RUF012

            async def aclose(self):
                return None

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            def build_request(self, *a, **k):
                return object()

            async def send(self, *a, **k):
                return _FakeResponse()

        with (
            patch(
                "infrastructure.safe_fetch.resolve_public_ip",
                AsyncMock(return_value="93.184.216.34"),
            ),
            patch("infrastructure.safe_fetch.httpx.AsyncClient", _FakeClient),
        ):
            result = await post_public("https://example.com/hook", "{}", headers={})
        assert result.status_code == 302
        assert "redirect" in result.error


class TestRetryAfter:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("5", 5.0),
            ("0", 0.0),
            (" 12 ", 12.0),
            ("1.5", 1.5),
            ("-3", None),  # negative is nonsense, ignore
            ("Wed, 21 Oct 2026 07:28:00 GMT", None),  # HTTP-date form not parsed
            ("soon", None),
            (None, None),
        ],
    )
    def test_parse_retry_after(self, header, expected):
        assert _parse_retry_after(header) == expected

    def test_post_result_defaults_to_no_retry_after(self):
        # Three-arg construction (every non-header call site) stays valid.
        assert PostResult(500, None, None).retry_after_seconds is None
