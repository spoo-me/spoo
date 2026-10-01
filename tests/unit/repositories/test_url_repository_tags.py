"""UrlRepository tag helpers and the TagRepository."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from bson import ObjectId

from .conftest import URL_OID, USER_OID, make_collection

T1 = ObjectId("1" * 24)


def _url_repo(col):
    from app.repositories.url_repository import UrlRepository

    return UrlRepository(col)


def _tag_repo(col):
    from app.repositories.tag_repository import TagRepository

    return TagRepository(col)


class TestApplyByIdsAndOwner:
    @pytest.mark.asyncio
    async def test_passes_raw_update_with_compound_filter(self):
        col = make_collection()
        col.update_many = AsyncMock(return_value=MagicMock(modified_count=2))
        update = {"$pull": {"tag_ids": {"$in": [T1]}}}

        count = await _url_repo(col).apply_by_ids_and_owner([URL_OID], USER_OID, update)

        assert count == 2
        query, sent = col.update_many.call_args[0]
        assert query == {"_id": {"$in": [URL_OID]}, "owner_id": USER_OID}
        assert sent == update

    @pytest.mark.asyncio
    async def test_refuses_missing_filters_or_empty_update(self):
        repo = _url_repo(make_collection())
        with pytest.raises(ValueError):
            await repo.apply_by_ids_and_owner([], USER_OID, {"$set": {"a": 1}})
        with pytest.raises(ValueError):
            await repo.apply_by_ids_and_owner([URL_OID], USER_OID, {})


class TestListIdsByOwnerAndTagIds:
    @pytest.mark.asyncio
    async def test_query_projection_and_ids(self):
        col = make_collection()
        cursor = col.find.return_value
        cursor.__aiter__.return_value = [{"_id": URL_OID}]

        ids = await _url_repo(col).list_ids_by_owner_and_tag_ids(USER_OID, [T1])

        assert ids == [URL_OID]
        query, projection = col.find.call_args[0]
        assert query == {"owner_id": USER_OID, "tag_ids": {"$in": [T1]}}
        assert projection == {"_id": 1}
        cursor.limit.assert_not_called()

    @pytest.mark.asyncio
    async def test_refuses_missing_owner_or_ids(self):
        repo = _url_repo(make_collection())
        with pytest.raises(ValueError):
            await repo.list_ids_by_owner_and_tag_ids(None, [T1])
        with pytest.raises(ValueError):
            await repo.list_ids_by_owner_and_tag_ids(USER_OID, [])


class TestCountTagIdsByOwner:
    @pytest.mark.asyncio
    async def test_pipeline_and_mapping(self):
        col = make_collection()
        col.aggregate.return_value.to_list = AsyncMock(
            return_value=[{"_id": T1, "count": 2}]
        )

        counts = await _url_repo(col).count_tag_ids_by_owner(USER_OID)

        assert counts == {T1: 2}
        pipeline = col.aggregate.call_args[0][0]
        assert pipeline[0] == {
            "$match": {"owner_id": USER_OID, "tag_ids.0": {"$exists": True}}
        }
        assert {"$unwind": "$tag_ids"} in pipeline


class TestPullTagIdByOwner:
    @pytest.mark.asyncio
    async def test_pulls_only_where_present(self):
        col = make_collection()
        col.update_many = AsyncMock(return_value=MagicMock(modified_count=3))

        assert await _url_repo(col).pull_tag_id_by_owner(USER_OID, T1) == 3
        query, update = col.update_many.call_args[0]
        assert query == {"owner_id": USER_OID, "tag_ids": T1}
        assert update == {"$pull": {"tag_ids": T1}}


class TestTagRepository:
    def _doc(self, name="launch"):
        return {
            "_id": T1,
            "owner_id": USER_OID,
            "name": name,
            "color": "violet",
            "icon": None,
            "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
        }

    @pytest.mark.asyncio
    async def test_find_by_ids_scopes_to_owner(self):
        col = make_collection()
        col.find.return_value.sort.return_value.to_list = AsyncMock(
            return_value=[self._doc()]
        )

        docs = await _tag_repo(col).find_by_ids_and_owner([T1], USER_OID)

        assert [d.name for d in docs] == ["launch"]
        assert col.find.call_args[0][0] == {"_id": {"$in": [T1]}, "owner_id": USER_OID}

    @pytest.mark.asyncio
    async def test_find_by_ids_empty_short_circuits(self):
        col = make_collection()
        assert await _tag_repo(col).find_by_ids_and_owner([], USER_OID) == []
        col.find.assert_not_called()

    @pytest.mark.asyncio
    async def test_find_by_names_scopes_to_owner(self):
        col = make_collection()
        col.find.return_value.sort.return_value.to_list = AsyncMock(return_value=[])

        await _tag_repo(col).find_by_names_and_owner(["launch"], USER_OID)

        assert col.find.call_args[0][0] == {
            "owner_id": USER_OID,
            "name": {"$in": ["launch"]},
        }

    @pytest.mark.asyncio
    async def test_update_sets_exactly_the_given_ops(self):
        col = make_collection()
        col.update_one = AsyncMock(return_value=MagicMock(matched_count=1))

        assert (
            await _tag_repo(col).update_by_id_and_owner(T1, USER_OID, {"name": "x"})
            is True
        )
        query, update = col.update_one.call_args[0]
        assert query == {"_id": T1, "owner_id": USER_OID}
        assert update == {"$set": {"name": "x"}}

    @pytest.mark.asyncio
    async def test_delete_by_owner_refuses_missing_owner(self):
        with pytest.raises(ValueError):
            await _tag_repo(make_collection()).delete_by_owner(None)
