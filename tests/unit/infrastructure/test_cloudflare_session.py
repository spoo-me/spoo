"""Tests for the shared Cloudflare transport session."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.infrastructure.cloudflare_session import CloudflareSession


def _response(status_code: int, headers: dict[str, str] | None = None) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.is_success = 200 <= status_code < 300
    response.text = "body"
    response.headers = headers or {}
    return response


def _session(http, **overrides) -> CloudflareSession:
    kwargs = {
        "http_client": http,
        "api_token": "tok",
        "base_url": "https://api.example/v4",
        "max_retries": 3,
        "initial_backoff_seconds": 0.001,
    }
    kwargs.update(overrides)
    return CloudflareSession(**kwargs)


class TestRequest:
    async def test_success_returns_immediately_with_auth_and_url(self):
        http = MagicMock()
        http.request = AsyncMock(return_value=_response(200))
        session = _session(http)

        response = await session.request("GET", "/zones/z1", params={"a": 1})

        assert response.status_code == 200
        http.request.assert_awaited_once()
        args = http.request.await_args
        assert args.args == ("GET", "https://api.example/v4/zones/z1")
        assert args.kwargs["headers"]["Authorization"] == "Bearer tok"
        assert args.kwargs["params"] == {"a": 1}

    async def test_host_header_override_for_local_emulator(self):
        """wrangler dev validates Host — containers must present localhost."""
        http = MagicMock()
        http.request = AsyncMock(return_value=_response(200))
        session = _session(http, host_header="localhost:8787")
        await session.request("PUT", "/x")
        headers = http.request.await_args.kwargs["headers"]
        assert headers["Host"] == "localhost:8787"

    async def test_no_host_header_by_default(self):
        http = MagicMock()
        http.request = AsyncMock(return_value=_response(200))
        await _session(http).request("GET", "/x")
        assert "Host" not in http.request.await_args.kwargs["headers"]

    async def test_4xx_returns_without_retry(self):
        """Client errors are the CALLER's business — no retry, no raise."""
        http = MagicMock()
        http.request = AsyncMock(return_value=_response(404))
        response = await _session(http).request("DELETE", "/thing")
        assert response.status_code == 404
        http.request.assert_awaited_once()

    async def test_5xx_retries_then_succeeds(self):
        http = MagicMock()
        http.request = AsyncMock(side_effect=[_response(503), _response(200)])
        response = await _session(http).request("GET", "/x")
        assert response.status_code == 200
        assert http.request.await_count == 2

    async def test_exhausted_5xx_returns_last_response(self):
        http = MagicMock()
        http.request = AsyncMock(return_value=_response(500))
        response = await _session(http).request("GET", "/x")
        assert response.status_code == 500
        assert http.request.await_count == 3  # max_retries

    async def test_transport_errors_retry_then_raise(self):
        http = MagicMock()
        http.request = AsyncMock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(httpx.ConnectError):
            await _session(http).request("GET", "/x")
        assert http.request.await_count == 3

    async def test_429_honours_retry_after(self):
        http = MagicMock()
        http.request = AsyncMock(
            side_effect=[
                _response(429, headers={"Retry-After": "7"}),
                _response(200),
            ]
        )
        with patch(
            "app.infrastructure.cloudflare_session.asyncio.sleep", new=AsyncMock()
        ) as sleep:
            response = await _session(http).request("GET", "/x")
        assert response.status_code == 200
        sleep.assert_awaited_once_with(7.0)

    async def test_malformed_retry_after_falls_back_to_backoff(self):
        http = MagicMock()
        http.request = AsyncMock(
            side_effect=[
                _response(429, headers={"Retry-After": "soon"}),
                _response(200),
            ]
        )
        with patch(
            "app.infrastructure.cloudflare_session.asyncio.sleep", new=AsyncMock()
        ) as sleep:
            await _session(http).request("GET", "/x")
        sleep.assert_awaited_once_with(0.001)  # initial backoff, attempt 0
