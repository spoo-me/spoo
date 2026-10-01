"""Unit tests for BlockedUrlRepository."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from .conftest import make_collection


class TestBlockedUrlRepository:
    def _repo(self, col=None):
        from app.repositories.blocked_url_repository import BlockedUrlRepository

        return BlockedUrlRepository(col or make_collection())

    @pytest.mark.asyncio
    async def test_get_patterns_returns_ids(self):
        col = make_collection()
        cursor = col.find.return_value
        cursor.to_list = AsyncMock(
            return_value=[{"_id": "pattern1"}, {"_id": "pattern2"}]
        )
        patterns = await self._repo(col).get_patterns()
        col.find.assert_called_once_with({}, {"_id": 1})
        assert patterns == ["pattern1", "pattern2"]

    @pytest.mark.asyncio
    async def test_get_patterns_returns_empty_list(self):
        col = make_collection()
        cursor = col.find.return_value
        cursor.to_list = AsyncMock(return_value=[])
        assert await self._repo(col).get_patterns() == []

    @pytest.mark.asyncio
    async def test_get_patterns_raises_on_db_error(self):
        col = make_collection()
        cursor = col.find.return_value
        cursor.to_list = AsyncMock(side_effect=Exception("db down"))
        with pytest.raises(Exception, match="db down"):
            await self._repo(col).get_patterns()
