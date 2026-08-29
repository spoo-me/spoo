"""Unit tests for UrlRepository."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId
from pymongo.errors import (
    DuplicateKeyError,
    OperationFailure,
    ServerSelectionTimeoutError,
)

from .conftest import URL_OID, USER_OID, _url_v2_doc, make_collection

DOMAIN = "spoo.me"


class TestUrlRepository:
    def _repo(self, col=None):
        from repositories.url_repository import UrlRepository

        return UrlRepository(col or make_collection())

    @pytest.mark.asyncio
    async def test_find_by_alias_returns_model(self):
        col = make_collection()
        col.find_one = AsyncMock(return_value=_url_v2_doc())
        result = await self._repo(col).find_by_alias("abc1234", DOMAIN)
        col.find_one.assert_awaited_once_with({"alias": "abc1234", "domain": DOMAIN})
        assert result is not None
        assert result.alias == "abc1234"

    @pytest.mark.asyncio
    async def test_find_by_alias_returns_none_on_miss(self):
        col = make_collection()
        col.find_one = AsyncMock(return_value=None)
        result = await self._repo(col).find_by_alias("missing", DOMAIN)
        assert result is None

    @pytest.mark.asyncio
    async def test_find_by_alias_scoped_to_domain(self):
        # Same alias under different domains is a different short — query
        # must include the domain so the compound unique index slot matches.
        col = make_collection()
        col.find_one = AsyncMock(return_value=None)
        await self._repo(col).find_by_alias("sale", "links.acme.com")
        col.find_one.assert_awaited_once_with(
            {"alias": "sale", "domain": "links.acme.com"}
        )

    @pytest.mark.asyncio
    async def test_find_by_id_returns_model(self):
        col = make_collection()
        col.find_one = AsyncMock(return_value=_url_v2_doc())
        result = await self._repo(col).find_by_id(URL_OID)
        col.find_one.assert_awaited_once_with({"_id": URL_OID})
        assert result is not None

    @pytest.mark.asyncio
    async def test_find_by_id_returns_none_on_miss(self):
        col = make_collection()
        col.find_one = AsyncMock(return_value=None)
        assert await self._repo(col).find_by_id(URL_OID) is None

    @pytest.mark.asyncio
    async def test_find_by_id_and_owner_scopes_query_to_owner(self):
        # Ownership must live IN the query — a foreign id has to answer
        # exactly like a missing one, so no unscoped read then check.
        col = make_collection()
        col.find_one = AsyncMock(return_value=_url_v2_doc())
        result = await self._repo(col).find_by_id_and_owner(URL_OID, USER_OID)
        col.find_one.assert_awaited_once_with({"_id": URL_OID, "owner_id": USER_OID})
        assert result is not None

    @pytest.mark.asyncio
    async def test_find_by_id_and_owner_returns_none_on_miss(self):
        col = make_collection()
        col.find_one = AsyncMock(return_value=None)
        assert await self._repo(col).find_by_id_and_owner(URL_OID, USER_OID) is None

    @pytest.mark.asyncio
    async def test_find_by_alias_and_owner_scopes_query(self):
        col = make_collection()
        col.find_one = AsyncMock(return_value=_url_v2_doc())
        result = await self._repo(col).find_by_alias_and_owner(
            "abc1234", DOMAIN, USER_OID
        )
        col.find_one.assert_awaited_once_with(
            {"alias": "abc1234", "domain": DOMAIN, "owner_id": USER_OID}
        )
        assert result is not None

    @pytest.mark.asyncio
    async def test_find_by_alias_and_owner_returns_none_on_miss(self):
        col = make_collection()
        col.find_one = AsyncMock(return_value=None)
        result = await self._repo(col).find_by_alias_and_owner(
            "missing", DOMAIN, USER_OID
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_insert_returns_id(self):
        col = make_collection()
        result_mock = MagicMock()
        result_mock.inserted_id = URL_OID
        col.insert_one = AsyncMock(return_value=result_mock)
        doc = {"alias": "abc1234", "long_url": "https://example.com"}
        oid = await self._repo(col).insert(doc)
        col.insert_one.assert_awaited_once_with(doc)
        assert oid == URL_OID

    @pytest.mark.asyncio
    async def test_update_returns_true_on_match(self):
        col = make_collection()
        result_mock = MagicMock(matched_count=1)
        col.update_one = AsyncMock(return_value=result_mock)
        ops = {"$set": {"status": "INACTIVE"}}
        ok = await self._repo(col).update(URL_OID, ops)
        col.update_one.assert_awaited_once_with({"_id": URL_OID}, ops)
        assert ok is True

    @pytest.mark.asyncio
    async def test_update_returns_false_on_no_match(self):
        col = make_collection()
        col.update_one = AsyncMock(return_value=MagicMock(matched_count=0))
        ok = await self._repo(col).update(URL_OID, {"$set": {"status": "INACTIVE"}})
        assert ok is False

    @pytest.mark.asyncio
    async def test_delete_returns_true_when_deleted(self):
        col = make_collection()
        col.delete_one = AsyncMock(return_value=MagicMock(deleted_count=1))
        ok = await self._repo(col).delete(URL_OID)
        col.delete_one.assert_awaited_once_with({"_id": URL_OID})
        assert ok is True

    @pytest.mark.asyncio
    async def test_delete_returns_false_when_not_found(self):
        col = make_collection()
        col.delete_one = AsyncMock(return_value=MagicMock(deleted_count=0))
        assert await self._repo(col).delete(URL_OID) is False

    @pytest.mark.asyncio
    async def test_check_alias_exists_true(self):
        col = make_collection()
        col.find_one = AsyncMock(return_value={"_id": URL_OID})
        assert await self._repo(col).check_alias_exists("abc1234", DOMAIN) is True
        col.find_one.assert_awaited_once_with(
            {"alias": "abc1234", "domain": DOMAIN}, {"_id": 1}
        )

    @pytest.mark.asyncio
    async def test_check_alias_exists_false(self):
        col = make_collection()
        col.find_one = AsyncMock(return_value=None)
        assert await self._repo(col).check_alias_exists("nope", DOMAIN) is False

    @pytest.mark.asyncio
    async def test_increment_clicks_uses_inc_and_set(self):
        col = make_collection()
        col.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
        ts = datetime(2024, 6, 1, tzinfo=timezone.utc)
        await self._repo(col).increment_clicks(URL_OID, last_click_time=ts)
        col.update_one.assert_awaited_once_with(
            {"_id": URL_OID},
            {"$inc": {"total_clicks": 1}, "$set": {"last_click": ts}},
        )

    @pytest.mark.asyncio
    async def test_increment_clicks_uses_now_when_no_time(self):
        col = make_collection()
        col.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
        await self._repo(col).increment_clicks(URL_OID)
        args = col.update_one.call_args
        update_doc = args[0][1]
        assert "$set" in update_doc
        assert isinstance(update_doc["$set"]["last_click"], datetime)

    @pytest.mark.asyncio
    async def test_expire_if_max_clicks_returns_true_when_expired(self):
        col = make_collection()
        col.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
        result = await self._repo(col).expire_if_max_clicks(URL_OID, 100)
        col.update_one.assert_awaited_once_with(
            {"_id": URL_OID, "total_clicks": {"$gte": 100}},
            {"$set": {"status": "EXPIRED"}},
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_expire_if_max_clicks_returns_false_when_not_reached(self):
        col = make_collection()
        col.update_one = AsyncMock(return_value=MagicMock(modified_count=0))
        assert await self._repo(col).expire_if_max_clicks(URL_OID, 100) is False

    @pytest.mark.asyncio
    async def test_expire_if_max_clicks_returns_false_when_already_expired(self):
        """matched but not modified — URL was already EXPIRED."""
        col = make_collection()
        col.update_one = AsyncMock(return_value=MagicMock(modified_count=0))
        assert await self._repo(col).expire_if_max_clicks(URL_OID, 100) is False

    @pytest.mark.asyncio
    async def test_expire_if_time_reached_flips_active_past_expiry(self):
        col = make_collection()
        col.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
        result = await self._repo(col).expire_if_time_reached(URL_OID)
        (filter_doc, update_doc) = col.update_one.call_args[0]
        assert filter_doc["_id"] == URL_OID
        # ACTIVE guard: BLOCKED/INACTIVE must never be clobbered, and the
        # flip stays idempotent between concurrent resolves.
        assert filter_doc["status"] == "ACTIVE"
        assert "$lte" in filter_doc["expire_after"]
        assert isinstance(filter_doc["expire_after"]["$lte"], datetime)
        assert update_doc == {"$set": {"status": "EXPIRED"}}
        assert result is True

    @pytest.mark.asyncio
    async def test_expire_if_time_reached_returns_false_when_not_flipped(self):
        """Not matched (future/absent expiry, non-ACTIVE) or already
        flipped by a concurrent resolve — either way, no side effects."""
        col = make_collection()
        col.update_one = AsyncMock(return_value=MagicMock(modified_count=0))
        assert await self._repo(col).expire_if_time_reached(URL_OID) is False

    @pytest.mark.asyncio
    async def test_find_by_owner_returns_models(self):
        col = make_collection()
        cursor = col.find.return_value
        cursor.to_list = AsyncMock(return_value=[_url_v2_doc()])
        query = {"owner_id": USER_OID}
        from schemas.models.url import UrlV2Doc

        docs = await self._repo(col).find_by_owner(query, "created_at", -1, 0, 20)
        col.find.assert_called_once_with(query)
        cursor.sort.assert_called_once_with("created_at", -1)
        cursor.sort.return_value.skip.assert_called_once_with(0)
        assert len(docs) == 1
        assert isinstance(docs[0], UrlV2Doc)

    @pytest.mark.asyncio
    async def test_count_by_query(self):
        col = make_collection()
        col.count_documents = AsyncMock(return_value=42)
        count = await self._repo(col).count_by_query({"owner_id": USER_OID})
        col.count_documents.assert_awaited_once_with({"owner_id": USER_OID})
        assert count == 42

    @pytest.mark.asyncio
    async def test_raises_on_db_error(self):
        col = make_collection()
        col.find_one = AsyncMock(side_effect=Exception("connection refused"))
        with pytest.raises(Exception, match="connection refused"):
            await self._repo(col).find_by_alias("any", DOMAIN)

    # ── Error path tests ──────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_find_by_alias_raises_on_operation_failure(self):
        """OperationFailure (e.g. query plan error) propagates from find_by_alias."""
        col = make_collection()
        col.find_one = AsyncMock(side_effect=OperationFailure("query failed"))
        with pytest.raises(OperationFailure):
            await self._repo(col).find_by_alias("abc", DOMAIN)

    @pytest.mark.asyncio
    async def test_find_by_alias_raises_on_server_timeout(self):
        """ServerSelectionTimeoutError (e.g. MongoDB unreachable) propagates."""
        col = make_collection()
        col.find_one = AsyncMock(side_effect=ServerSelectionTimeoutError("timed out"))
        with pytest.raises(ServerSelectionTimeoutError):
            await self._repo(col).find_by_alias("abc", DOMAIN)

    @pytest.mark.asyncio
    async def test_insert_raises_duplicate_key(self):
        """DuplicateKeyError on insert propagates (alias unique index violation)."""
        col = make_collection()
        col.insert_one = AsyncMock(
            side_effect=DuplicateKeyError("E11000 duplicate key")
        )
        with pytest.raises(DuplicateKeyError):
            await self._repo(col).insert({"alias": "abc1234"})

    @pytest.mark.asyncio
    async def test_insert_raises_on_operation_failure(self):
        """OperationFailure on insert propagates."""
        col = make_collection()
        col.insert_one = AsyncMock(side_effect=OperationFailure("write failed"))
        with pytest.raises(OperationFailure):
            await self._repo(col).insert({"alias": "abc1234"})

    @pytest.mark.asyncio
    async def test_update_raises_on_server_timeout(self):
        """ServerSelectionTimeoutError during update propagates."""
        col = make_collection()
        col.update_one = AsyncMock(side_effect=ServerSelectionTimeoutError("timed out"))
        with pytest.raises(ServerSelectionTimeoutError):
            await self._repo(col).update(URL_OID, {"$set": {"status": "INACTIVE"}})

    @pytest.mark.asyncio
    async def test_non_pymongo_error_propagates_uncaught(self):
        """Errors outside pymongo (e.g. ValueError) are NOT caught by the repo."""
        col = make_collection()
        col.find_one = AsyncMock(side_effect=ValueError("unexpected"))
        with pytest.raises(ValueError, match="unexpected"):
            await self._repo(col).find_by_alias("abc", DOMAIN)


class TestUrlRepositoryBulkDelete:
    def _repo(self, col=None):
        from repositories.url_repository import UrlRepository

        return UrlRepository(col or make_collection())

    @pytest.mark.asyncio
    async def test_list_aliases_returns_list_of_strings(self):
        col = make_collection()
        cursor = MagicMock()
        cursor.to_list = AsyncMock(
            return_value=[{"alias": "abc"}, {"alias": "xyz"}, {"alias": "qq"}]
        )
        col.find = MagicMock(return_value=cursor)
        result = await self._repo(col).list_aliases_by_owner_and_domain(
            USER_OID, "links.acme.com"
        )
        assert result == ["abc", "xyz", "qq"]
        col.find.assert_called_once()
        # Both filters must be present in the query.
        query = col.find.call_args[0][0]
        assert query["owner_id"] == USER_OID
        assert query["domain"] == "links.acme.com"

    @pytest.mark.asyncio
    async def test_delete_many_returns_count(self):
        col = make_collection()
        col.delete_many = AsyncMock(return_value=MagicMock(deleted_count=7))
        count = await self._repo(col).delete_many_by_owner_and_domain(
            USER_OID, "links.acme.com"
        )
        assert count == 7
        col.delete_many.assert_awaited_once()
        query = col.delete_many.call_args[0][0]
        assert query == {"owner_id": USER_OID, "domain": "links.acme.com"}

    @pytest.mark.asyncio
    async def test_delete_many_refuses_missing_filters(self):
        repo = self._repo()
        with pytest.raises(ValueError):
            await repo.delete_many_by_owner_and_domain(None, "links.acme.com")
        with pytest.raises(ValueError):
            await repo.delete_many_by_owner_and_domain(USER_OID, "")


class TestUrlRepositoryBulkByIds:
    def _repo(self, col=None):
        from repositories.url_repository import UrlRepository

        return UrlRepository(col or make_collection())

    @pytest.mark.asyncio
    async def test_find_by_ids_scopes_ownership_in_query(self):
        col = make_collection()
        cursor = MagicMock()
        cursor.to_list = AsyncMock(return_value=[_url_v2_doc()])
        col.find = MagicMock(return_value=cursor)
        ids = [URL_OID]
        result = await self._repo(col).find_by_ids_and_owner(ids, USER_OID)
        assert len(result) == 1
        query = col.find.call_args[0][0]
        # Ownership folded into the query — never a post-fetch compare.
        assert query == {"_id": {"$in": ids}, "owner_id": USER_OID}

    @pytest.mark.asyncio
    async def test_find_by_ids_refuses_missing_filters(self):
        repo = self._repo()
        with pytest.raises(ValueError):
            await repo.find_by_ids_and_owner([], USER_OID)
        with pytest.raises(ValueError):
            await repo.find_by_ids_and_owner([URL_OID], None)

    @pytest.mark.asyncio
    async def test_delete_by_ids_compound_filter_and_count(self):
        col = make_collection()
        col.delete_many = AsyncMock(return_value=MagicMock(deleted_count=2))
        ids = [URL_OID]
        count = await self._repo(col).delete_by_ids_and_owner(ids, USER_OID)
        assert count == 2
        query = col.delete_many.call_args[0][0]
        assert query == {"_id": {"$in": ids}, "owner_id": USER_OID}

    @pytest.mark.asyncio
    async def test_delete_by_ids_refuses_missing_filters(self):
        repo = self._repo()
        with pytest.raises(ValueError):
            await repo.delete_by_ids_and_owner([], USER_OID)
        with pytest.raises(ValueError):
            await repo.delete_by_ids_and_owner([URL_OID], None)

    @pytest.mark.asyncio
    async def test_update_by_ids_wraps_set_and_scopes_ownership(self):
        col = make_collection()
        col.update_many = AsyncMock(return_value=MagicMock(modified_count=3))
        ids = [URL_OID]
        now = datetime(2026, 7, 14, tzinfo=timezone.utc)
        count = await self._repo(col).update_by_ids_and_owner(
            ids, USER_OID, {"status": "INACTIVE", "updated_at": now}
        )
        assert count == 3
        query, ops = col.update_many.call_args[0]
        assert query == {"_id": {"$in": ids}, "owner_id": USER_OID}
        assert ops == {"$set": {"status": "INACTIVE", "updated_at": now}}

    @pytest.mark.asyncio
    async def test_update_by_ids_refuses_missing_filters_or_empty_ops(self):
        repo = self._repo()
        with pytest.raises(ValueError):
            await repo.update_by_ids_and_owner([], USER_OID, {"status": "ACTIVE"})
        with pytest.raises(ValueError):
            await repo.update_by_ids_and_owner([URL_OID], None, {"status": "ACTIVE"})
        with pytest.raises(ValueError):
            await repo.update_by_ids_and_owner([URL_OID], USER_OID, {})

    @pytest.mark.asyncio
    async def test_update_by_ids_duplicate_key_propagates_without_error_log(self):
        """The alias-race DuplicateKeyError is an expected, caller-handled
        outcome (bulk domain move maps it to a per-item conflict) — it must
        not emit the repo-layer error event that genuine failures do."""
        col = make_collection()
        col.update_many = AsyncMock(side_effect=DuplicateKeyError("dup"))
        with (
            patch("repositories.url_repository.log") as log_mock,
            pytest.raises(DuplicateKeyError),
        ):
            await self._repo(col).update_by_ids_and_owner(
                [URL_OID], USER_OID, {"domain": "links.acme.com"}
            )
        log_mock.error.assert_not_called()


class TestSweepQueries:
    """These ride the async aggregate helper — a raw `.to_list()` on the
    un-awaited cursor silently returns nothing (caught in a live run)."""

    @pytest.mark.asyncio
    async def test_list_active_hosts_by_registrable_awaits_aggregate(self):
        from repositories.url_repository import UrlRepository

        col = AsyncMock()
        col.name = "urlsV2"
        cursor = MagicMock()
        cursor.to_list = AsyncMock(
            return_value=[{"_id": "a.evil.com", "sample_url": "https://a.evil.com/x"}]
        )
        col.aggregate = AsyncMock(return_value=cursor)

        result = await UrlRepository(col).list_active_hosts_by_registrable("evil.com")

        assert result == [("a.evil.com", "https://a.evil.com/x")]
        pipeline = col.aggregate.await_args.args[0]
        assert pipeline[0]["$match"]["dest.registrable_domain"] == "evil.com"
        assert pipeline[0]["$match"]["status"] == "ACTIVE"

    @pytest.mark.asyncio
    async def test_list_recent_destination_hosts_awaits_aggregate(self):
        from datetime import datetime, timezone

        from repositories.url_repository import UrlRepository

        col = AsyncMock()
        col.name = "urlsV2"
        cursor = MagicMock()
        cursor.to_list = AsyncMock(
            return_value=[{"_id": "fresh.com", "sample_url": "https://fresh.com/a"}]
        )
        col.aggregate = AsyncMock(return_value=cursor)

        result = await UrlRepository(col).list_recent_destination_hosts(
            datetime(2026, 8, 1, tzinfo=timezone.utc)
        )

        assert result == [("fresh.com", "https://fresh.com/a")]
        pipeline = col.aggregate.await_args.args[0]
        assert "$gte" in pipeline[0]["$match"]["_id"]


class TestBlockActiveByAliases:
    def _repo(self, col):
        from repositories.url_repository import UrlRepository

        col.name = "urlsV2"
        return UrlRepository(col)

    @pytest.mark.asyncio
    async def test_stamps_audit_trail_on_specific_pairs(self):
        col = make_collection()
        col.update_many = AsyncMock(return_value=MagicMock(modified_count=2))
        repo = self._repo(col)
        n = await repo.block_active_by_aliases(
            [("abc", "spoo.me"), ("xyz", "go.acme.com")], reason="compromised"
        )
        assert n == 2
        flt, ops = col.update_many.await_args.args
        assert flt["$or"] == [
            {"alias": "abc", "domain": "spoo.me"},
            {"alias": "xyz", "domain": "go.acme.com"},
        ]
        assert flt["status"] == "ACTIVE"
        assert ops["$set"]["status"] == "BLOCKED"
        assert ops["$set"]["blocked_reason"] == "compromised"
        assert ops["$set"]["blocked_at"] is not None

    @pytest.mark.asyncio
    async def test_empty_pairs_never_hits_the_db(self):
        col = make_collection()
        repo = self._repo(col)
        assert await repo.block_active_by_aliases([], reason="r") == 0
        col.update_many.assert_not_awaited()


class TestDestinationHistory:
    def _repo(self, col):
        from repositories.url_repository import UrlRepository

        col.name = "urlsV2"
        return UrlRepository(col)

    @pytest.mark.asyncio
    async def test_empty_host_returns_zeroed_shape(self):
        col = make_collection()
        col.aggregate.return_value.to_list = AsyncMock(return_value=[])
        hist = await self._repo(col).destination_history("nobody.com")
        assert hist["link_count"] == 0
        assert hist["first_seen"] is None

    @pytest.mark.asyncio
    async def test_splits_anon_and_owned_and_counts_owners(self):
        from schemas.models.base import ANONYMOUS_OWNER_ID

        col = make_collection()
        first = ObjectId()
        col.aggregate.return_value.to_list = AsyncMock(
            return_value=[
                {
                    "link_count": 5,
                    "anon_count": 3,
                    "total_clicks": 120,
                    "distinct_owners": [ANONYMOUS_OWNER_ID, ObjectId(), ObjectId()],
                    "first_seen": first,
                    "edited_count": 1,
                }
            ]
        )
        hist = await self._repo(col).destination_history("busy.com")
        assert hist["link_count"] == 5
        assert hist["anon_count"] == 3
        assert hist["owned_count"] == 2
        # ANONYMOUS_OWNER_ID is excluded from the distinct-owner count.
        assert hist["distinct_owners"] == 2
        assert hist["total_clicks"] == 120
        assert hist["first_seen"] == first.generation_time.isoformat()
        assert hist["edited_count"] == 1


class TestHostBreadth:
    def _repo(self, col):
        from repositories.url_repository import UrlRepository

        col.name = "urlsV2"
        return UrlRepository(col)

    @pytest.mark.asyncio
    async def test_reports_shared_platform_shape(self):
        col = make_collection()
        col.aggregate.return_value.to_list = AsyncMock(
            return_value=[
                {
                    "total": 380,
                    "blocked": 3,
                    "paths": [f"https://sites.google.com/view/p{i}" for i in range(40)],
                    "creators": [ObjectId() for _ in range(12)],
                }
            ]
        )
        b = await self._repo(col).host_breadth("sites.google.com")
        assert b["total_links"] == 380
        assert b["blocked_links"] == 3
        assert b["distinct_urls"] == 40
        assert b["distinct_creators"] == 12
        assert len(b["sample_urls"]) == 8  # capped sample, not the whole set

    @pytest.mark.asyncio
    async def test_unknown_host_is_zeroed(self):
        col = make_collection()
        col.aggregate.return_value.to_list = AsyncMock(return_value=[])
        b = await self._repo(col).host_breadth("nobody.example")
        assert b["total_links"] == 0 and b["sample_urls"] == []
