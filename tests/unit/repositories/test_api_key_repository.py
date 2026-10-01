"""Unit tests for ApiKeyRepository."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from .conftest import KEY_OID, USER_OID, _api_key_doc, make_collection


class TestApiKeyRepository:
    def _repo(self, col=None):
        from app.repositories.api_key_repository import ApiKeyRepository

        return ApiKeyRepository(col or make_collection())

    @pytest.mark.asyncio
    async def test_insert_returns_id(self):
        col = make_collection()
        col.insert_one = AsyncMock(return_value=MagicMock(inserted_id=KEY_OID))
        oid = await self._repo(col).insert({"token_hash": "abc", "user_id": USER_OID})
        assert oid == KEY_OID

    @pytest.mark.asyncio
    async def test_find_by_hash_returns_model(self):
        col = make_collection()
        col.find_one = AsyncMock(return_value=_api_key_doc())
        result = await self._repo(col).find_by_hash("deadbeef" * 8)
        col.find_one.assert_awaited_once_with({"token_hash": "deadbeef" * 8})
        assert result is not None
        assert result.revoked is False

    @pytest.mark.asyncio
    async def test_find_by_hash_returns_none(self):
        col = make_collection()
        col.find_one = AsyncMock(return_value=None)
        assert await self._repo(col).find_by_hash("nope") is None

    @pytest.mark.asyncio
    async def test_list_by_user_returns_models(self):
        col = make_collection()
        cursor = col.find.return_value
        cursor.sort.return_value = cursor
        cursor.to_list = AsyncMock(return_value=[_api_key_doc()])
        result = await self._repo(col).list_by_user(USER_OID)
        col.find.assert_called_once_with({"user_id": USER_OID})
        cursor.sort.assert_called_once_with("created_at", 1)
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_revoke_soft(self):
        col = make_collection()
        col.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
        result = await self._repo(col).revoke(USER_OID, KEY_OID, hard_delete=False)
        col.update_one.assert_awaited_once_with(
            {"_id": KEY_OID, "user_id": USER_OID},
            {"$set": {"revoked": True}},
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_revoke_hard_delete(self):
        col = make_collection()
        col.delete_one = AsyncMock(return_value=MagicMock(deleted_count=1))
        result = await self._repo(col).revoke(USER_OID, KEY_OID, hard_delete=True)
        col.delete_one.assert_awaited_once_with({"_id": KEY_OID, "user_id": USER_OID})
        assert result is True

    @pytest.mark.asyncio
    async def test_revoke_returns_false_on_not_found(self):
        col = make_collection()
        col.update_one = AsyncMock(return_value=MagicMock(matched_count=0))
        assert await self._repo(col).revoke(USER_OID, KEY_OID) is False

    @pytest.mark.asyncio
    async def test_count_by_user(self):
        col = make_collection()
        col.count_documents = AsyncMock(return_value=3)
        count = await self._repo(col).count_by_user(USER_OID)
        col.count_documents.assert_awaited_once_with(
            {"user_id": USER_OID, "revoked": {"$ne": True}}
        )
        assert count == 3

    # ── Account erasure ───────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_delete_by_user_hard_deletes_revoked_keys_too(self):
        col = make_collection()
        result = MagicMock()
        result.deleted_count = 2
        col.delete_many = AsyncMock(return_value=result)
        count = await self._repo(col).delete_by_user(USER_OID)
        # No revoked filter — erasure ignores the soft-delete flag.
        col.delete_many.assert_awaited_once_with({"user_id": USER_OID})
        assert count == 2
