"""Unit tests for BlockedDomainRepository."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.repositories.blocked_domain_repository import BlockedDomainRepository


class TestBlockedDomainRepository:
    @pytest.mark.asyncio
    async def test_is_blocked_returns_true_on_hit(self):
        col = AsyncMock()
        col.find_one = AsyncMock(return_value={"_id": "evil.com"})
        col.name = "blocked_domains"
        repo = BlockedDomainRepository(col)
        assert await repo.is_blocked("evil.com") is True
        col.find_one.assert_awaited_once_with({"_id": "evil.com"}, {"_id": 1})

    @pytest.mark.asyncio
    async def test_is_blocked_returns_false_on_miss(self):
        col = AsyncMock()
        col.find_one = AsyncMock(return_value=None)
        col.name = "blocked_domains"
        repo = BlockedDomainRepository(col)
        assert await repo.is_blocked("safe.example.com") is False

    @pytest.mark.asyncio
    async def test_list_all_returns_set_of_ids(self):
        col = AsyncMock()
        cursor = MagicMock()
        cursor.to_list = AsyncMock(
            return_value=[{"_id": "evil.com"}, {"_id": "phishing.example.com"}]
        )
        col.find = MagicMock(return_value=cursor)
        col.name = "blocked_domains"
        repo = BlockedDomainRepository(col)

        result = await repo.list_all()
        assert result == {"evil.com", "phishing.example.com"}
        col.find.assert_called_once_with({}, {"_id": 1})
