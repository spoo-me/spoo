"""Unit tests for ClickRepository."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from pymongo.errors import OperationFailure, ServerSelectionTimeoutError

from .conftest import URL_OID, USER_OID, make_collection


class TestClickRepository:
    def _repo(self, col=None):
        from app.repositories.click_repository import ClickRepository

        return ClickRepository(col or make_collection())

    @pytest.mark.asyncio
    async def test_insert_calls_insert_one(self):
        col = make_collection()
        col.insert_one = AsyncMock(return_value=MagicMock())
        doc = {
            "meta": {"url_id": URL_OID, "short_code": "abc1234", "owner_id": USER_OID},
            "clicked_at": datetime.now(timezone.utc),
            "browser": "Chrome",
        }
        result = await self._repo(col).insert(doc)
        col.insert_one.assert_awaited_once_with(doc)
        assert result is None

    @pytest.mark.asyncio
    async def test_insert_raises_on_db_error(self):
        col = make_collection()
        col.insert_one = AsyncMock(side_effect=Exception("write error"))
        with pytest.raises(Exception, match="write error"):
            await self._repo(col).insert(
                {"meta": {}, "clicked_at": datetime.now(timezone.utc)}
            )

    @pytest.mark.asyncio
    async def test_aggregate_returns_list(self):
        col = make_collection()
        cursor = col.aggregate.return_value
        cursor.to_list = AsyncMock(return_value=[{"result": 1}])
        pipeline = [{"$match": {"meta.url_id": URL_OID}}]
        result = await self._repo(col).aggregate(pipeline)
        col.aggregate.assert_called_once_with(pipeline)
        assert result == [{"result": 1}]

    @pytest.mark.asyncio
    async def test_aggregate_raises_on_db_error(self):
        col = make_collection()
        cursor = col.aggregate.return_value
        cursor.to_list = AsyncMock(side_effect=Exception("agg failed"))
        with pytest.raises(Exception, match="agg failed"):
            await self._repo(col).aggregate([])

    # ── Error path tests ──────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_insert_raises_on_server_timeout(self):
        """ServerSelectionTimeoutError (MongoDB unreachable) propagates from insert."""
        col = make_collection()
        col.insert_one = AsyncMock(side_effect=ServerSelectionTimeoutError("timed out"))
        with pytest.raises(ServerSelectionTimeoutError):
            await self._repo(col).insert(
                {"meta": {}, "clicked_at": datetime.now(timezone.utc)}
            )

    @pytest.mark.asyncio
    async def test_aggregate_raises_on_operation_failure(self):
        """OperationFailure (e.g. invalid pipeline stage) propagates from aggregate."""
        col = make_collection()
        col.aggregate = AsyncMock(side_effect=OperationFailure("pipeline failed"))
        with pytest.raises(OperationFailure):
            await self._repo(col).aggregate([{"$match": {}}])

    @pytest.mark.asyncio
    async def test_aggregate_raises_on_server_timeout(self):
        """ServerSelectionTimeoutError during aggregation propagates."""
        col = make_collection()
        cursor = col.aggregate.return_value
        cursor.to_list = AsyncMock(side_effect=ServerSelectionTimeoutError("timed out"))
        with pytest.raises(ServerSelectionTimeoutError):
            await self._repo(col).aggregate([{"$match": {}}])

    # ── Account erasure ───────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_delete_by_owner_uses_metafield_only_predicate(self):
        col = make_collection()
        result = MagicMock()
        result.deleted_count = 7
        col.delete_many = AsyncMock(return_value=result)
        count = await self._repo(col).delete_by_owner(USER_OID)
        # Time-series delete: the predicate MUST stay meta.owner_id-only.
        col.delete_many.assert_awaited_once_with({"meta.owner_id": USER_OID})
        assert count == 7

    @pytest.mark.asyncio
    async def test_delete_by_owner_refuses_anonymous_sentinel(self):
        from app.schemas.models.base import ANONYMOUS_OWNER_ID

        col = make_collection()
        with pytest.raises(ValueError):
            await self._repo(col).delete_by_owner(ANONYMOUS_OWNER_ID)
        col.delete_many.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delete_by_url_ids_uses_metafield_only_predicate(self):
        col = make_collection()
        result = MagicMock()
        result.deleted_count = 3
        col.delete_many = AsyncMock(return_value=result)
        count = await self._repo(col).delete_by_url_ids([URL_OID])
        # meta.url_id lives in the metaField subdoc — still a legal
        # time-series delete predicate.
        col.delete_many.assert_awaited_once_with({"meta.url_id": {"$in": [URL_OID]}})
        assert count == 3

    @pytest.mark.asyncio
    async def test_delete_by_url_ids_empty_list_is_a_noop(self):
        col = make_collection()
        assert await self._repo(col).delete_by_url_ids([]) == 0
        col.delete_many.assert_not_awaited()
