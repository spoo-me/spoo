"""Unit tests for AppGrantRepository."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from bson import ObjectId
from pymongo.errors import PyMongoError

from app.repositories.app_grant_repository import AppGrantRepository

from .conftest import USER_OID, make_collection


class _AsyncIter:
    """Minimal async iterator wrapping a list for mocking cursor iteration."""

    def __init__(self, items):
        self._items = iter(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._items)
        except StopIteration:
            raise StopAsyncIteration from None


_APP_ID = "spoo-snap"
_GRANT_OID = ObjectId("eeeeeeeeeeeeeeeeeeeeeeee")


def _grant_doc(
    revoked: bool = False,
    app_id: str = _APP_ID,
    last_used: datetime | None = None,
) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "_id": _GRANT_OID,
        "user_id": USER_OID,
        "app_id": app_id,
        "granted_at": now,
        "last_used_at": last_used,
        "revoked_at": now if revoked else None,
    }


def _repo(col=None) -> AppGrantRepository:
    return AppGrantRepository(col or make_collection())


# ── find_active_grant ─────────────────────────────────────────────────────────


class TestFindActiveGrant:
    @pytest.mark.asyncio
    async def test_returns_model_when_found(self):
        col = make_collection()
        col.find_one = AsyncMock(return_value=_grant_doc())
        result = await _repo(col).find_active_grant(USER_OID, _APP_ID)
        col.find_one.assert_awaited_once_with(
            {"user_id": USER_OID, "app_id": _APP_ID, "revoked_at": None}
        )
        assert result is not None
        assert result.app_id == _APP_ID
        assert result.user_id == USER_OID

    @pytest.mark.asyncio
    async def test_returns_none_on_miss(self):
        col = make_collection()
        col.find_one = AsyncMock(return_value=None)
        assert await _repo(col).find_active_grant(USER_OID, _APP_ID) is None

    @pytest.mark.asyncio
    async def test_propagates_pymongo_error(self):
        col = make_collection()
        col.find_one = AsyncMock(side_effect=PyMongoError("conn lost"))
        with pytest.raises(PyMongoError):
            await _repo(col).find_active_grant(USER_OID, _APP_ID)


# ── find_active_for_user ──────────────────────────────────────────────────────


class TestFindActiveForUser:
    @pytest.mark.asyncio
    async def test_returns_list(self):
        col = make_collection()
        docs = [_grant_doc(), _grant_doc(app_id="spoo-desktop")]
        col.find = MagicMock(return_value=_AsyncIter(docs))

        results = await _repo(col).find_active_for_user(USER_OID)
        col.find.assert_called_once_with({"user_id": USER_OID, "revoked_at": None})
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_returns_empty_list(self):
        col = make_collection()
        col.find = MagicMock(return_value=_AsyncIter([]))

        results = await _repo(col).find_active_for_user(USER_OID)
        assert results == []

    @pytest.mark.asyncio
    async def test_propagates_pymongo_error(self):
        col = make_collection()
        col.find = MagicMock(side_effect=PyMongoError("fail"))
        with pytest.raises(PyMongoError):
            await _repo(col).find_active_for_user(USER_OID)


# ── find_all_for_user ─────────────────────────────────────────────────────────


class TestFindAllForUser:
    @pytest.mark.asyncio
    async def test_includes_revoked(self):
        col = make_collection()
        docs = [_grant_doc(), _grant_doc(revoked=True)]
        col.find = MagicMock(return_value=_AsyncIter(docs))

        results = await _repo(col).find_all_for_user(USER_OID)
        col.find.assert_called_once_with({"user_id": USER_OID})
        assert len(results) == 2


# ── create_or_reactivate ──────────────────────────────────────────────────────


class TestCreateOrReactivate:
    @pytest.mark.asyncio
    async def test_upserts_and_returns_model(self):
        col = make_collection()
        col.find_one_and_update = AsyncMock(return_value=_grant_doc())
        result = await _repo(col).create_or_reactivate(USER_OID, _APP_ID, scopes=None)

        call_args = col.find_one_and_update.await_args
        assert call_args[0][0] == {"user_id": USER_OID, "app_id": _APP_ID}
        update = call_args[0][1]
        assert "granted_at" in update["$set"]
        assert update["$set"]["revoked_at"] is None
        assert update["$setOnInsert"]["user_id"] == USER_OID
        assert call_args[1]["upsert"] is True
        assert call_args[1]["return_document"] is True

        assert result is not None
        assert result.app_id == _APP_ID

    @pytest.mark.asyncio
    async def test_propagates_pymongo_error(self):
        col = make_collection()
        col.find_one_and_update = AsyncMock(side_effect=PyMongoError("dup"))
        with pytest.raises(PyMongoError):
            await _repo(col).create_or_reactivate(USER_OID, _APP_ID, scopes=None)


# ── revoke ────────────────────────────────────────────────────────────────────


class TestRevoke:
    @pytest.mark.asyncio
    async def test_returns_true_when_revoked(self):
        col = make_collection()
        col.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
        assert await _repo(col).revoke(USER_OID, _APP_ID) is True
        call_args = col.update_one.await_args[0]
        assert call_args[0] == {
            "user_id": USER_OID,
            "app_id": _APP_ID,
            "revoked_at": None,
        }

    @pytest.mark.asyncio
    async def test_returns_false_when_no_grant(self):
        col = make_collection()
        col.update_one = AsyncMock(return_value=MagicMock(modified_count=0))
        assert await _repo(col).revoke(USER_OID, _APP_ID) is False

    @pytest.mark.asyncio
    async def test_propagates_pymongo_error(self):
        col = make_collection()
        col.update_one = AsyncMock(side_effect=PyMongoError("fail"))
        with pytest.raises(PyMongoError):
            await _repo(col).revoke(USER_OID, _APP_ID)


# ── touch_last_used ───────────────────────────────────────────────────────────


class TestTouchLastUsed:
    @pytest.mark.asyncio
    async def test_updates_active_grant(self):
        col = make_collection()
        col.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
        await _repo(col).touch_last_used(USER_OID, _APP_ID)
        call_args = col.update_one.await_args[0]
        assert call_args[0] == {
            "user_id": USER_OID,
            "app_id": _APP_ID,
            "revoked_at": None,
        }
        assert "last_used_at" in call_args[1]["$set"]

    @pytest.mark.asyncio
    async def test_propagates_pymongo_error(self):
        col = make_collection()
        col.update_one = AsyncMock(side_effect=PyMongoError("fail"))
        with pytest.raises(PyMongoError):
            await _repo(col).touch_last_used(USER_OID, _APP_ID)


class TestDeleteByUser:
    @pytest.mark.asyncio
    async def test_hard_deletes_revoked_grants_too(self):
        col = make_collection()
        result = MagicMock()
        result.deleted_count = 3
        col.delete_many = AsyncMock(return_value=result)
        count = await _repo(col).delete_by_user(USER_OID)
        # No revoked_at filter — erasure ignores the soft-delete flag.
        col.delete_many.assert_awaited_once_with({"user_id": USER_OID})
        assert count == 3
