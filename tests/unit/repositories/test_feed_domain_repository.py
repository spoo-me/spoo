"""Unit tests for FeedDomainRepository — the full-swap feed refresh."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.repositories.feed_domain_repository import FeedDomainRepository


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


def _add_col(upserted_id):
    col = MagicMock()
    col.update_one = AsyncMock(return_value=MagicMock(upserted_id=upserted_id))
    return col


class TestAdd:
    """The write behind a self-applied list proposal. A model-emitted string
    becomes a Mongo key here, so it is reduced to the registrable domain or
    refused; anything else is an inert row contains() can never match."""

    @pytest.mark.asyncio
    async def test_new_domain_upserts_the_feed_key_and_reports_new(self):
        col = _add_col(upserted_id="redirectors:forms.gle")
        assert await FeedDomainRepository(col).add("redirectors", "Forms.GLE.") is True
        filt, update = col.update_one.await_args.args
        assert filt == {"_id": "redirectors:forms.gle"}
        assert update["$set"]["feed"] == "redirectors"
        assert update["$set"]["domain"] == "forms.gle"
        assert "synced_at" in update["$set"]
        assert col.update_one.await_args.kwargs == {"upsert": True}

    @pytest.mark.asyncio
    async def test_a_url_shaped_proposal_is_reduced_to_its_domain(self):
        col = _add_col(upserted_id="shorteners:waa.ai")
        assert (
            await FeedDomainRepository(col).add("shorteners", "https://waa.ai/57589")
            is True
        )
        assert col.update_one.await_args.args[0] == {"_id": "shorteners:waa.ai"}

    @pytest.mark.asyncio
    async def test_existing_domain_reports_not_new(self):
        assert (
            await FeedDomainRepository(_add_col(None)).add("shorteners", "bit.ly")
            is False
        )

    @pytest.mark.asyncio
    async def test_garbage_writes_nothing(self):
        col = _add_col(None)
        repo = FeedDomainRepository(col)
        assert await repo.add("shorteners", " . ") is False
        assert await repo.add("shorteners", "not a host") is False
        col.update_one.assert_not_awaited()
