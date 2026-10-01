"""Unit tests for hot-link screening (the hotness detector's abuse consumer)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.click.consumers.hotness import HotUrl
from app.services.safety.hot import HotLinkScreen


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


class TestHotLinkScreenSecondaryDestinations:
    @pytest.mark.asyncio
    async def test_geo_destinations_each_get_an_event(self):
        doc = MagicMock(
            long_url="https://clean.example/",
            geo_rules={
                "IN": "https://hidden.example/kit",
                "US": "https://clean.example/us",
            },
        )
        screen, _u, sink = _screen(v2_doc=doc)

        await screen.on_hot(_hot())

        hosts = [c.args[0].host for c in sink.emit.await_args_list]
        assert hosts == ["clean.example", "hidden.example"]
        event = sink.emit.await_args_list[1].args[0]
        assert event.url == "https://hidden.example/kit"
        assert event.context["link_destinations"] == [
            "https://clean.example/",
            "https://hidden.example/kit",
            "https://clean.example/us",
        ]

    @pytest.mark.asyncio
    async def test_verdicted_geo_host_is_skipped_but_main_still_screened(self):
        doc = MagicMock(
            long_url="https://clean.example/",
            geo_rules={"IN": "https://known.example/"},
        )
        screen, _u, sink = _screen(v2_doc=doc)
        screen._verdict_repo.find_by_host = AsyncMock(
            side_effect=lambda h: MagicMock() if h == "known.example" else None
        )

        await screen.on_hot(_hot())

        assert [c.args[0].host for c in sink.emit.await_args_list] == ["clean.example"]

    @pytest.mark.asyncio
    async def test_variant_destinations_each_get_an_event(self):
        from app.schemas.models.url import AbVariant

        doc = MagicMock(
            long_url="https://clean.example/",
            geo_rules=None,
            ab_variants=[
                AbVariant(url="https://variant.example/b", weight=50),
                AbVariant(url="https://clean.example/c", weight=20),
            ],
        )
        screen, _u, sink = _screen(v2_doc=doc)

        await screen.on_hot(_hot())

        hosts = [c.args[0].host for c in sink.emit.await_args_list]
        assert hosts == ["clean.example", "variant.example"]
        event = sink.emit.await_args_list[1].args[0]
        assert event.context["link_destinations"] == [
            "https://clean.example/",
            "https://variant.example/b",
            "https://clean.example/c",
        ]

    @pytest.mark.asyncio
    async def test_single_destination_carries_no_link_destinations(self):
        doc = MagicMock(long_url="https://only.example/", geo_rules=None)
        screen, _u, sink = _screen(v2_doc=doc)
        await screen.on_hot(_hot())
        assert "link_destinations" not in sink.emit.await_args.args[0].context

    @pytest.mark.asyncio
    async def test_pre_start_destination_is_screened_too(self):
        doc = MagicMock(
            long_url="https://clean.example/",
            geo_rules=None,
            pre_start_url="https://teaser.example/soon",
        )
        screen, _u, sink = _screen(v2_doc=doc)
        await screen.on_hot(_hot())
        hosts = [c.args[0].host for c in sink.emit.await_args_list]
        assert hosts == ["clean.example", "teaser.example"]

    @pytest.mark.asyncio
    async def test_after_expiry_destination_is_screened_too(self):
        doc = MagicMock(
            long_url="https://clean.example/",
            geo_rules=None,
            pre_start_url=None,
            expired_redirect_url="https://ended.example/bye",
        )
        screen, _u, sink = _screen(v2_doc=doc)
        await screen.on_hot(_hot())
        hosts = [c.args[0].host for c in sink.emit.await_args_list]
        assert hosts == ["clean.example", "ended.example"]
