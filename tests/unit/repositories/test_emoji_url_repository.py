"""Unit tests for EmojiUrlRepository."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call

import pytest
from pymongo.errors import WriteError

from .conftest import _legacy_url_doc, make_collection


class TestEmojiUrlRepository:
    def _repo(self, col=None):
        from repositories.legacy.emoji_url_repository import EmojiUrlRepository

        return EmojiUrlRepository(col or make_collection())

    def _emoji_doc(self):
        return {**_legacy_url_doc(), "_id": "\U0001f525\U0001f4af"}

    @pytest.mark.asyncio
    async def test_find_by_id_returns_model(self):
        col = make_collection()
        col.find_one = AsyncMock(return_value=self._emoji_doc())
        result = await self._repo(col).find_by_id("\U0001f525\U0001f4af")
        col.find_one.assert_awaited_once_with({"_id": "\U0001f525\U0001f4af"})
        assert result is not None

    @pytest.mark.asyncio
    async def test_find_by_id_returns_none(self):
        col = make_collection()
        col.find_one = AsyncMock(return_value=None)
        assert await self._repo(col).find_by_id("\U0001f525\U0001f4af") is None

    @pytest.mark.asyncio
    async def test_insert_prepends_id(self):
        col = make_collection()
        col.insert_one = AsyncMock(return_value=MagicMock())
        data = {"url": "https://example.com"}
        await self._repo(col).insert("\U0001f525\U0001f4af", data)
        col.insert_one.assert_awaited_once_with({"_id": "\U0001f525\U0001f4af", **data})

    @pytest.mark.asyncio
    async def test_update_passes_doc_unchanged(self):
        col = make_collection()
        col.update_one = AsyncMock(return_value=MagicMock())
        ops = {"$inc": {"total-clicks": 1}, "$set": {"last-click": "2024-01-01"}}
        await self._repo(col).update("\U0001f525\U0001f4af", ops)
        col.update_one.assert_awaited_once_with({"_id": "\U0001f525\U0001f4af"}, ops)

    @pytest.mark.asyncio
    async def test_update_retries_with_inc_only_on_document_overflow(self):
        col = make_collection()
        update_ops = {
            "$inc": {
                "total-clicks": 1,
                "counter.2024-01-15": 1,
                "country.US.counts": 1,
            },
            "$set": {
                "last-click": "2024-01-15 12:00:00",
                "last-click-browser": "Chrome",
            },
            "$addToSet": {"ips": "1.2.3.4", "country.US.ips": "1.2.3.4"},
        }
        col.update_one = AsyncMock(
            side_effect=[WriteError("too large", code=10334), MagicMock()]
        )
        await self._repo(col).update("\U0001f525\U0001f4af", update_ops)
        col.update_one.assert_has_awaits(
            [
                call({"_id": "\U0001f525\U0001f4af"}, update_ops),
                call({"_id": "\U0001f525\U0001f4af"}, {"$inc": {"total-clicks": 1}}),
            ],
            any_order=False,
        )

    @pytest.mark.asyncio
    async def test_check_exists_true(self):
        col = make_collection()
        col.find_one = AsyncMock(return_value={"_id": "\U0001f525\U0001f4af"})
        assert await self._repo(col).check_exists("\U0001f525\U0001f4af") is True

    @pytest.mark.asyncio
    async def test_check_exists_false(self):
        col = make_collection()
        col.find_one = AsyncMock(return_value=None)
        assert await self._repo(col).check_exists("\u274c") is False

    @pytest.mark.asyncio
    async def test_check_exists_vs16_insensitive_queries_regex(self):
        col = make_collection()
        col.find_one = AsyncMock(return_value={"_id": "\u2b50\ufe0f\U0001f389"})
        repo = self._repo(col)

        assert await repo.check_exists_vs16_insensitive("\u2b50\U0001f389") is True

        query, projection = col.find_one.await_args.args
        pattern = query["_id"]["$regex"]
        # The emitted pattern must match the canonical form and every
        # VS16-decorated variant of it, and nothing else. Assert in
        # BYTES mode: Mongo's PCRE matches bytewise, and only bytes-mode
        # re distinguishes the correct grouped `(?:VS16)?` from the buggy
        # ungrouped form (which str-mode matching lets through).
        import re

        compiled = re.compile(pattern.encode())
        assert compiled.fullmatch("\u2b50\U0001f389".encode())
        assert compiled.fullmatch("\u2b50\ufe0f\U0001f389".encode())
        assert not compiled.fullmatch("\U0001f389\u2b50".encode())
        assert projection == {"_id": 1}

    @pytest.mark.asyncio
    async def test_check_exists_vs16_insensitive_false(self):
        col = make_collection()
        col.find_one = AsyncMock(return_value=None)
        repo = self._repo(col)
        assert await repo.check_exists_vs16_insensitive("\u2b50") is False

    @pytest.mark.asyncio
    async def test_aggregate_returns_first_result(self):
        col = make_collection()
        cursor = col.aggregate.return_value
        cursor.to_list = AsyncMock(return_value=[{"total": 5}])
        result = await self._repo(col).aggregate(
            [{"$group": {"_id": None, "total": {"$sum": 1}}}]
        )
        assert result == {"total": 5}

    @pytest.mark.asyncio
    async def test_aggregate_returns_none_on_empty(self):
        col = make_collection()
        cursor = col.aggregate.return_value
        cursor.to_list = AsyncMock(return_value=[])
        assert await self._repo(col).aggregate([]) is None


class TestCountByDestHost:
    @pytest.mark.asyncio
    async def test_filters_on_stamped_dest_host(self):
        from repositories.legacy.emoji_url_repository import EmojiUrlRepository

        col = AsyncMock()
        col.name = "emojis"
        col.count_documents = AsyncMock(return_value=3)
        repo = EmojiUrlRepository(col)
        assert await repo.count_by_dest_host("evil.com") == 3
        col.count_documents.assert_awaited_once_with({"dest.host": "evil.com"})


class TestEmojiSafetyBlockSurface:
    """The emoji repo deliberately does not inherit LegacyUrlRepository —
    these pins keep its hand-mirrored safety surface from drifting."""

    def _repo(self, col):
        from repositories.legacy.emoji_url_repository import EmojiUrlRepository

        col.name = "emojis"
        return EmojiUrlRepository(col)

    @pytest.mark.asyncio
    async def test_block_by_dest_host_mirrors_legacy(self):
        col = make_collection()
        col.update_many = AsyncMock(return_value=MagicMock(modified_count=2))
        assert await self._repo(col).block_by_dest_host("evil.com", reason="phish") == 2
        flt, ops = col.update_many.await_args.args
        assert flt == {"dest.host": "evil.com", "blocked": {"$ne": True}}
        assert ops["$set"]["blocked_reason"] == "phish"

    @pytest.mark.asyncio
    async def test_list_by_dest_host_is_status_blind(self):
        col = make_collection()
        col.find.return_value.to_list = AsyncMock(
            return_value=[{"_id": "⭐️🎉", "url": "https://evil.com/a"}]
        )
        assert await self._repo(col).list_by_dest_host("evil.com") == [
            ("⭐️🎉", "https://evil.com/a")
        ]
        assert col.find.call_args.args[0] == {"dest.host": "evil.com"}

    @pytest.mark.asyncio
    async def test_unblock(self):
        col = make_collection()
        col.update_one = AsyncMock(return_value=MagicMock(modified_count=0))
        assert await self._repo(col).unblock("⭐") is False
