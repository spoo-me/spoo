"""Lifecycle tests for the edge KV write-through (custom meta-tags)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from app.services.edge_cache.og_writethrough import OgEdgeWritethrough
from tests.factories import make_url_cache

SYSTEM = "spoo.me"

META = dict(
    meta_title="Custom Card",
    meta_description="Desc",
    meta_image="https://cdn.example.com/og.png",
    meta_color="#FF5733",
)


def _kv() -> AsyncMock:
    kv = AsyncMock()
    kv.put = AsyncMock(return_value=True)
    kv.delete = AsyncMock(return_value=True)
    return kv


def _wt(kv, ttl_seconds: int = 86_400) -> OgEdgeWritethrough:
    return OgEdgeWritethrough(kv, system_domain=SYSTEM, ttl_seconds=ttl_seconds)


class TestSync:
    @pytest.mark.asyncio
    async def test_active_og_link_puts_og_only_entry_with_ttl(self):
        kv = _kv()
        await _wt(kv, ttl_seconds=3600).sync(make_url_cache(**META))
        kv.put.assert_awaited_once()
        key, value = kv.put.call_args.args
        assert key == "cache:spoo.me:abc1234"
        entry = json.loads(value)
        assert entry["type"] == "og_only"
        assert "og:title" in entry["og_html"]
        assert 'content="Custom Card"' in entry["og_html"]
        assert "url" not in entry  # og_only carries no destination
        # TTL is the backstop that heals a missed delete / out-of-band block.
        assert kv.put.call_args.kwargs["expiration_ttl"] == 3600

    @pytest.mark.asyncio
    async def test_rendered_html_reflects_long_url_host(self):
        kv = _kv()
        await _wt(kv).sync(
            make_url_cache(**META, long_url="https://new-destination.example/x")
        )
        entry = json.loads(kv.put.call_args.args[1])
        assert "new-destination.example" in entry["og_html"]

    @pytest.mark.asyncio
    async def test_meta_cleared_deletes_key(self):
        kv = _kv()
        await _wt(kv).sync(make_url_cache())  # no meta_title
        kv.put.assert_not_called()
        kv.delete.assert_awaited_once_with("cache:spoo.me:abc1234")

    @pytest.mark.asyncio
    async def test_non_active_status_deletes_key(self):
        kv = _kv()
        await _wt(kv).sync(make_url_cache(**META, url_status="BLOCKED"))
        kv.put.assert_not_called()
        kv.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_tenant_domain_is_skipped(self):
        # Worker never runs on tenant hostnames — KV entries would be dead weight.
        kv = _kv()
        await _wt(kv).sync(make_url_cache(**META, domain="links.acme.com"))
        kv.put.assert_not_called()
        kv.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_kv_failure_never_raises(self):
        kv = _kv()
        kv.put.side_effect = RuntimeError("cf down")
        await _wt(kv).sync(make_url_cache(**META))  # must not raise

    @pytest.mark.asyncio
    async def test_block_bots_og_link_omits_destination_from_html(self):
        kv = _kv()
        await _wt(kv).sync(make_url_cache(**META, block_bots=True))
        entry = json.loads(kv.put.call_args.args[1])
        assert "https://example.com" not in entry["og_html"]


class TestRemove:
    @pytest.mark.asyncio
    async def test_remove_deletes_key(self):
        kv = _kv()
        await _wt(kv).remove(SYSTEM, "oldalias")
        kv.delete.assert_awaited_once_with("cache:spoo.me:oldalias")

    @pytest.mark.asyncio
    async def test_remove_skips_tenant_domain(self):
        kv = _kv()
        await _wt(kv).remove("links.acme.com", "x")
        kv.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_remove_failure_never_raises(self):
        kv = _kv()
        kv.delete.side_effect = RuntimeError("cf down")
        await _wt(kv).remove(SYSTEM, "x")  # must not raise
