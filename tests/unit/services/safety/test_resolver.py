"""Unit tests for terminal-URL resolution."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from infrastructure.safe_fetch import FetchHardError
from services.safety.resolver import resolve_terminal_url


def _client(responses):
    client = AsyncMock()
    client.head = AsyncMock(side_effect=responses)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


class TestResolveTerminalUrl:
    @pytest.mark.asyncio
    async def test_walks_to_the_terminal(self):
        responses = [
            SimpleNamespace(
                status_code=301,
                is_redirect=True,
                headers={"location": "https://landing.example/page"},
            ),
            SimpleNamespace(status_code=200, is_redirect=False, headers={}),
        ]
        with (
            patch(
                "services.safety.resolver.resolve_public_ip",
                AsyncMock(return_value="93.184.216.34"),
            ),
            patch(
                "services.safety.resolver.httpx.AsyncClient",
                return_value=_client(responses),
            ),
        ):
            assert (
                await resolve_terminal_url("https://t.co/abc")
                == "https://landing.example/page"
            )

    @pytest.mark.asyncio
    async def test_private_hop_is_unresolved(self):
        with patch(
            "services.safety.resolver.resolve_public_ip",
            AsyncMock(side_effect=FetchHardError("not public")),
        ):
            assert await resolve_terminal_url("https://t.co/abc") is None

    @pytest.mark.asyncio
    async def test_loop_hits_the_hop_ceiling(self):
        loop = SimpleNamespace(
            status_code=302,
            is_redirect=True,
            headers={"location": "https://loop.example/again"},
        )
        with (
            patch(
                "services.safety.resolver.resolve_public_ip",
                AsyncMock(return_value="93.184.216.34"),
            ),
            patch(
                "services.safety.resolver.httpx.AsyncClient",
                return_value=_client([loop] * 10),
            ),
        ):
            assert await resolve_terminal_url("https://loop.example/start") is None

    @pytest.mark.asyncio
    async def test_non_http_is_unresolved(self):
        assert await resolve_terminal_url("ftp://x.example/f") is None
