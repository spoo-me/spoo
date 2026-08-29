"""Unit tests for FeedDomainRepository — the full-swap feed refresh."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from repositories.feed_domain_repository import FeedDomainRepository


def _col() -> AsyncMock:
    col = AsyncMock()
    col.name = "safety_feed_domains"
    col.bulk_write = AsyncMock()
    col.delete_many = AsyncMock(return_value=MagicMock(deleted_count=7))
    col.distinct = AsyncMock(return_value=[])
    return col


class TestReplaceFeed:
    @pytest.mark.asyncio
    async def test_upserts_normalised_domains_then_purges_stale(self):
        col = _col()
        repo = FeedDomainRepository(col)

        kept, purged, new = await repo.replace_feed(
            "fishfish", ["Evil.COM.", "scam.net", "", "  "]
        )

        assert kept == 2
        assert purged == 7
        assert new == {"evil.com", "scam.net"}
        ops = col.bulk_write.await_args.args[0]
        first = ops[0]._doc["$set"]
        assert first == {
            "feed": "fishfish",
            "domain": "evil.com",
            "synced_at": first["synced_at"],
        }
        assert ops[0]._filter == {"_id": "fishfish:evil.com"}
        # Purge is scoped to this feed and to docs older than this sync.
        purge_filter = col.delete_many.await_args.args[0]
        assert purge_filter["feed"] == "fishfish"
        assert "$lt" in purge_filter["synced_at"]

    @pytest.mark.asyncio
    async def test_empty_download_never_wipes_the_set(self):
        col = _col()
        repo = FeedDomainRepository(col)

        kept, purged, new = await repo.replace_feed("fishfish", [])

        assert (kept, purged, new) == (0, 0, set())
        col.bulk_write.assert_not_awaited()
        col.delete_many.assert_not_awaited()


class TestContains:
    @pytest.mark.asyncio
    async def test_point_lookup_on_composite_id(self):
        col = _col()
        col.find_one = AsyncMock(return_value={"_id": "fishfish:evil.com"})
        repo = FeedDomainRepository(col)

        assert await repo.contains("fishfish", "Evil.com.") is True
        col.find_one.assert_awaited_once_with({"_id": "fishfish:evil.com"}, {"_id": 1})

    @pytest.mark.asyncio
    async def test_miss_returns_false(self):
        col = _col()
        col.find_one = AsyncMock(return_value=None)
        assert await FeedDomainRepository(col).contains("fishfish", "ok.com") is False
