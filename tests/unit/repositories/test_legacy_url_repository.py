"""Unit tests for LegacyUrlRepository."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call

import pytest
from pymongo.errors import WriteError

from .conftest import _legacy_url_doc, make_collection


class TestLegacyUrlRepository:
    def _repo(self, col=None):
        from repositories.legacy.legacy_url_repository import LegacyUrlRepository

        return LegacyUrlRepository(col or make_collection())

    @pytest.mark.asyncio
    async def test_find_by_id_returns_model(self):
        col = make_collection()
        col.find_one = AsyncMock(return_value=_legacy_url_doc())
        result = await self._repo(col).find_by_id("abc123")
        col.find_one.assert_awaited_once_with({"_id": "abc123"})
        assert result is not None
        assert result.url == "https://example.com"

    @pytest.mark.asyncio
    async def test_find_by_id_returns_none(self):
        col = make_collection()
        col.find_one = AsyncMock(return_value=None)
        assert await self._repo(col).find_by_id("missing") is None

    @pytest.mark.asyncio
    async def test_insert_prepends_id(self):
        col = make_collection()
        col.insert_one = AsyncMock(return_value=MagicMock())
        url_data = {"url": "https://example.com", "total-clicks": 0}
        await self._repo(col).insert("abc123", url_data)
        col.insert_one.assert_awaited_once_with({"_id": "abc123", **url_data})

    @pytest.mark.asyncio
    async def test_update_passes_update_doc_unchanged(self):
        col = make_collection()
        col.update_one = AsyncMock(return_value=MagicMock())
        # Exact v1 click tracking update document — must be passed through unmodified
        update_ops = {
            "$inc": {
                "total-clicks": 1,
                "counter.2024-01-15": 1,
                "country.US.counts": 1,
                "browser.Chrome.counts": 1,
                "os_name.Linux.counts": 1,
                "referrer.example.com.counts": 1,
            },
            "$set": {
                "last-click": "15 Jan 2024 12:00:00",
                "last-click-browser": "Chrome",
                "last-click-os": "Linux",
                "last-click-country": "US",
                "average_redirection_time": 0.05,
            },
            "$addToSet": {
                "ips": "1.2.3.4",
                "country.US.ips": "1.2.3.4",
                "browser.Chrome.ips": "1.2.3.4",
                "os_name.Linux.ips": "1.2.3.4",
                "referrer.example.com.ips": "1.2.3.4",
            },
        }
        await self._repo(col).update("abc123", update_ops)
        col.update_one.assert_awaited_once_with({"_id": "abc123"}, update_ops)

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
        # First call raises WriteError with code 10334 (document too large), retry succeeds
        col.update_one = AsyncMock(
            side_effect=[WriteError("too large", code=10334), MagicMock()]
        )
        await self._repo(col).update("abc123", update_ops)
        col.update_one.assert_has_awaits(
            [
                call({"_id": "abc123"}, update_ops),
                call({"_id": "abc123"}, {"$inc": {"total-clicks": 1}}),
            ],
            any_order=False,
        )

    @pytest.mark.asyncio
    async def test_check_exists_true(self):
        col = make_collection()
        col.find_one = AsyncMock(return_value={"_id": "abc123"})
        assert await self._repo(col).check_exists("abc123") is True
        col.find_one.assert_awaited_once_with({"_id": "abc123"}, {"_id": 1})

    @pytest.mark.asyncio
    async def test_check_exists_false(self):
        col = make_collection()
        col.find_one = AsyncMock(return_value=None)
        assert await self._repo(col).check_exists("missing") is False

    @pytest.mark.asyncio
    async def test_aggregate_returns_first_result(self):
        col = make_collection()
        cursor = col.aggregate.return_value
        cursor.to_list = AsyncMock(return_value=[{"result": "data"}])
        pipeline = [{"$match": {"_id": "abc123"}}]
        result = await self._repo(col).aggregate(pipeline)
        col.aggregate.assert_called_once_with(pipeline)
        assert result == {"result": "data"}

    @pytest.mark.asyncio
    async def test_aggregate_returns_none_on_empty(self):
        col = make_collection()
        cursor = col.aggregate.return_value
        cursor.to_list = AsyncMock(return_value=[])
        assert await self._repo(col).aggregate([]) is None


class TestCountByDestHost:
    @pytest.mark.asyncio
    async def test_filters_on_stamped_dest_host(self):
        from repositories.legacy.legacy_url_repository import LegacyUrlRepository

        col = AsyncMock()
        col.name = "urls"
        col.count_documents = AsyncMock(return_value=2)
        repo = LegacyUrlRepository(col)
        assert await repo.count_by_dest_host("evil.com") == 2
        col.count_documents.assert_awaited_once_with({"dest.host": "evil.com"})


class TestSafetyBlockSurface:
    def _repo(self, col):
        from repositories.legacy.legacy_url_repository import LegacyUrlRepository

        col.name = "urls"
        return LegacyUrlRepository(col)

    @pytest.mark.asyncio
    async def test_list_by_dest_host_is_status_blind(self):
        col = make_collection()
        col.find.return_value.limit.return_value.to_list = AsyncMock(
            return_value=[
                {"_id": "aaa111", "url": "https://evil.com/a"},
                {"_id": "bbb222", "url": "https://evil.com/b"},
            ]
        )
        rows = await self._repo(col).list_by_dest_host("evil.com")
        assert rows == [
            ("aaa111", "https://evil.com/a"),
            ("bbb222", "https://evil.com/b"),
        ]
        # No blocked filter: this doubles as the eviction set.
        assert col.find.call_args.args[0] == {"dest.host": "evil.com"}

    @pytest.mark.asyncio
    async def test_block_stamps_audit_trail_and_never_restamps(self):
        col = make_collection()
        result = MagicMock(modified_count=4)
        col.update_many = AsyncMock(return_value=result)
        assert await self._repo(col).block_by_dest_host("evil.com", reason="phish") == 4
        flt, ops = col.update_many.await_args.args
        # $ne in the filter = a re-block never overwrites the original stamp.
        assert flt == {"dest.host": "evil.com", "blocked": {"$ne": True}}
        assert ops["$set"]["blocked"] is True
        assert ops["$set"]["blocked_reason"] == "phish"
        assert ops["$set"]["blocked_at"] is not None

    @pytest.mark.asyncio
    async def test_unblock_removes_flag_keeps_stamps(self):
        col = make_collection()
        col.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
        assert await self._repo(col).unblock("aaa111") is True
        flt, ops = col.update_one.await_args.args
        assert flt == {"_id": "aaa111", "blocked": True}
        # Only the flag goes; the audit stamps survive the reversal.
        assert set(ops["$unset"]) == {"blocked"}
        assert "unblocked_at" in ops["$set"]
