"""Unit tests for TokenRepository."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from .conftest import TOKEN_OID, USER_OID, make_collection


class TestTokenRepository:
    def _repo(self, col=None):
        from app.repositories.token_repository import TokenRepository

        return TokenRepository(col or make_collection())

    @pytest.mark.asyncio
    async def test_create_returns_id(self):
        col = make_collection()
        col.insert_one = AsyncMock(return_value=MagicMock(inserted_id=TOKEN_OID))
        oid = await self._repo(col).create({"token_hash": "abc"})
        assert oid == TOKEN_OID

    @pytest.mark.asyncio
    async def test_mark_as_used_sets_used_at(self):
        col = make_collection()
        col.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
        result = await self._repo(col).mark_as_used(TOKEN_OID)
        assert col.update_one.await_count == 1
        call_args = col.update_one.call_args[0]
        assert call_args[0] == {"_id": TOKEN_OID}
        assert "$set" in call_args[1]
        assert isinstance(call_args[1]["$set"]["used_at"], datetime)
        assert result is True

    @pytest.mark.asyncio
    async def test_mark_as_used_returns_false_on_miss(self):
        col = make_collection()
        col.update_one = AsyncMock(return_value=MagicMock(matched_count=0))
        assert await self._repo(col).mark_as_used(TOKEN_OID) is False

    @pytest.mark.asyncio
    async def test_find_valid_by_hash_reads_without_consuming(self):
        # The look-before-flip half of restore_with_token: same liveness
        # filter as consume_by_hash, but no write.
        col = make_collection()
        col.find_one = AsyncMock(return_value=None)
        result = await self._repo(col).find_valid_by_hash("h" * 64, "deletion_restore")
        assert result is None
        query = col.find_one.call_args[0][0]
        assert query["token_hash"] == "h" * 64
        assert query["token_type"] == "deletion_restore"
        assert query["used_at"] is None
        assert isinstance(query["expires_at"]["$gt"], datetime)
        col.find_one_and_update.assert_not_called()
        col.update_one.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_by_user_no_type_filter(self):
        col = make_collection()
        col.delete_many = AsyncMock(return_value=MagicMock(deleted_count=2))
        count = await self._repo(col).delete_by_user(USER_OID)
        col.delete_many.assert_awaited_once_with({"user_id": USER_OID})
        assert count == 2

    @pytest.mark.asyncio
    async def test_delete_by_user_with_type_filter(self):
        col = make_collection()
        col.delete_many = AsyncMock(return_value=MagicMock(deleted_count=1))
        count = await self._repo(col).delete_by_user(USER_OID, "email_verify")
        col.delete_many.assert_awaited_once_with(
            {"user_id": USER_OID, "token_type": "email_verify"}
        )
        assert count == 1

    @pytest.mark.asyncio
    async def test_count_recent_builds_correct_query(self):
        col = make_collection()
        col.count_documents = AsyncMock(return_value=2)
        count = await self._repo(col).count_recent(USER_OID, "email_verify", minutes=30)
        assert col.count_documents.await_count == 1
        query = col.count_documents.call_args[0][0]
        assert query["user_id"] == USER_OID
        assert query["token_type"] == "email_verify"
        assert "$gte" in query["created_at"]
        assert count == 2


class TestTokenRepositoryErasure:
    def _repo(self, col=None):
        from app.repositories.token_repository import TokenRepository

        return TokenRepository(col or make_collection())

    @pytest.mark.asyncio
    async def test_delete_by_user_or_email_matches_either(self):
        col = make_collection()
        result = MagicMock()
        result.deleted_count = 3
        col.delete_many = AsyncMock(return_value=result)
        count = await self._repo(col).delete_by_user_or_email(
            USER_OID, "user@example.com"
        )
        col.delete_many.assert_awaited_once_with(
            {"$or": [{"user_id": USER_OID}, {"email": "user@example.com"}]}
        )
        assert count == 3

    @pytest.mark.asyncio
    async def test_delete_by_hash_targets_exact_token_only(self):
        """Precision cleanup: hash + type, never a user-wide sweep — a
        concurrent request's freshly-minted token must survive."""
        col = make_collection()
        result = MagicMock()
        result.deleted_count = 1
        col.delete_many = AsyncMock(return_value=result)
        count = await self._repo(col).delete_by_hash("h" * 64, "deletion_restore")
        col.delete_many.assert_awaited_once_with(
            {"token_hash": "h" * 64, "token_type": "deletion_restore"}
        )
        assert count == 1

    @pytest.mark.asyncio
    async def test_delete_by_user_or_email_drops_empty_email_clause(self):
        """{"email": ""} would match OTHER accounts' tokens stored with an
        empty address — a falsy email keeps only the user_id clause."""
        col = make_collection()
        result = MagicMock()
        result.deleted_count = 1
        col.delete_many = AsyncMock(return_value=result)
        count = await self._repo(col).delete_by_user_or_email(USER_OID, "")
        col.delete_many.assert_awaited_once_with({"user_id": USER_OID})
        assert count == 1
