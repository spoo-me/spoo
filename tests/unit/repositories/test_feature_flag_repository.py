"""Unit tests for FeatureFlagRepository."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from bson import ObjectId
from pymongo.errors import PyMongoError

from .conftest import make_collection

FLAG_OID = ObjectId("eeeeeeeeeeeeeeeeeeeeeeee")


def _flag_doc(name: str = "custom_domains") -> dict:
    return {
        "_id": FLAG_OID,
        "name": name,
        "enabled": True,
        "rollout_type": "allowlist",
        "allowlist_user_ids": [],
        "allowlist_emails": ["zingzy@spoo.me"],
        "percentage": 0,
        "enabled_digits": [],
        "tier": None,
        "description": "test",
        "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
    }


class TestFeatureFlagRepository:
    def _repo(self, col=None):
        from app.repositories.feature_flag_repository import FeatureFlagRepository

        return FeatureFlagRepository(col or make_collection())

    @pytest.mark.asyncio
    async def test_find_by_name_returns_model(self):
        col = make_collection()
        col.find_one = AsyncMock(return_value=_flag_doc())
        result = await self._repo(col).find_by_name("custom_domains")
        col.find_one.assert_awaited_once_with({"name": "custom_domains"})
        assert result is not None
        assert result.name == "custom_domains"
        assert result.enabled is True

    @pytest.mark.asyncio
    async def test_find_by_name_returns_none_on_miss(self):
        col = make_collection()
        col.find_one = AsyncMock(return_value=None)
        result = await self._repo(col).find_by_name("missing")
        assert result is None

    @pytest.mark.asyncio
    async def test_upsert_inserts_when_missing(self):
        col = make_collection()
        result_mock = MagicMock(upserted_id=FLAG_OID)
        col.update_one = AsyncMock(return_value=result_mock)
        oid = await self._repo(col).upsert("new_flag", {"enabled": True})
        col.update_one.assert_awaited_once()
        assert oid == FLAG_OID

    @pytest.mark.asyncio
    async def test_upsert_returns_existing_id_when_no_insert(self):
        col = make_collection()
        # update_one matched but did not insert
        col.update_one = AsyncMock(return_value=MagicMock(upserted_id=None))
        col.find_one = AsyncMock(return_value={"_id": FLAG_OID})
        oid = await self._repo(col).upsert("existing", {"enabled": True})
        assert oid == FLAG_OID

    @pytest.mark.asyncio
    async def test_list_all_returns_models(self):
        col = make_collection()
        col.find.return_value.to_list = AsyncMock(
            return_value=[_flag_doc("a"), _flag_doc("b")]
        )
        result = await self._repo(col).list_all()
        assert len(result) == 2
        assert {r.name for r in result} == {"a", "b"}

    @pytest.mark.asyncio
    async def test_list_all_empty(self):
        col = make_collection()
        col.find.return_value.to_list = AsyncMock(return_value=[])
        result = await self._repo(col).list_all()
        assert result == []


class TestPullAllowlisted:
    def _repo(self, col=None):
        from app.repositories.feature_flag_repository import FeatureFlagRepository

        return FeatureFlagRepository(col or make_collection())

    @pytest.mark.asyncio
    async def test_pulls_id_and_both_email_casings_from_every_flag(self):
        col = make_collection()
        result = MagicMock()
        result.modified_count = 2
        col.update_many = AsyncMock(return_value=result)
        uid = ObjectId("aaaaaaaaaaaaaaaaaaaaaaaa")
        count = await self._repo(col).pull_allowlisted(uid, "User@Example.com")
        col.update_many.assert_awaited_once_with(
            {},
            {
                "$pull": {
                    "allowlist_user_ids": uid,
                    "allowlist_emails": {
                        "$in": ["User@Example.com", "user@example.com"]
                    },
                }
            },
        )
        assert count == 2

    @pytest.mark.asyncio
    async def test_propagates_pymongo_error(self):
        col = make_collection()
        col.update_many = AsyncMock(side_effect=PyMongoError("conn lost"))
        with pytest.raises(PyMongoError):
            await self._repo(col).pull_allowlisted(
                ObjectId("aaaaaaaaaaaaaaaaaaaaaaaa"), "user@example.com"
            )
