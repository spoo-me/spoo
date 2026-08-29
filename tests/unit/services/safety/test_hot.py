"""Unit tests for hot-link screening (the hotness detector's abuse consumer)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.click.consumers.hotness import HotUrl
from services.safety.hot import HotLinkScreen


def _hot(short_code="abc1234", domain="default", count=50) -> HotUrl:
    return HotUrl(domain=domain, short_code=short_code, count=count, window_bucket=1)


def _screen(*, v2_doc=None, legacy_doc=None, verdict=None):
    url_repo = AsyncMock()
    url_repo.find_by_alias = AsyncMock(return_value=v2_doc)
    legacy_repo = AsyncMock()
    legacy_repo.find_by_id = AsyncMock(return_value=legacy_doc)
    verdict_repo = AsyncMock()
    verdict_repo.find_by_host = AsyncMock(return_value=verdict)
    sink = AsyncMock()
    screen = HotLinkScreen(
        url_repo, legacy_repo, verdict_repo, sink, system_default_domain="spoo.me"
    )
    return screen, url_repo, sink


class TestHotLinkScreen:
    @pytest.mark.asyncio
    async def test_unverdicted_hot_destination_gets_screened(self):
        doc = MagicMock(long_url="https://fresh-scam.com/kit")
        screen, url_repo, sink = _screen(v2_doc=doc)

        await screen.on_hot(_hot())

        # "default" from the click event maps to the system domain.
        url_repo.find_by_alias.assert_awaited_once_with("abc1234", "spoo.me")
        event = sink.emit.await_args.args[0]
        assert event.trigger == "hot"
        assert event.host == "fresh-scam.com"
        assert event.context["clicks_in_window"] == 50

    @pytest.mark.asyncio
    async def test_verdicted_host_is_skipped(self):
        doc = MagicMock(long_url="https://known.com/x")
        screen, _u, sink = _screen(v2_doc=doc, verdict=MagicMock())

        await screen.on_hot(_hot())

        sink.emit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_falls_back_to_the_legacy_collection(self):
        legacy = MagicMock(url="https://old-scam.com/y")
        screen, _u, sink = _screen(legacy_doc=legacy)

        await screen.on_hot(_hot(short_code="oldcode"))

        assert sink.emit.await_args.args[0].host == "old-scam.com"

    @pytest.mark.asyncio
    async def test_unknown_link_and_errors_are_silent(self):
        screen, _u, sink = _screen()
        await screen.on_hot(_hot())
        sink.emit.assert_not_awaited()

        screen2, url_repo2, sink2 = _screen()
        url_repo2.find_by_alias = AsyncMock(side_effect=RuntimeError("mongo down"))
        await screen2.on_hot(_hot())
        sink2.emit.assert_not_awaited()
