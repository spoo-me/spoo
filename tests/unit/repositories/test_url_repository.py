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
        from app.repositories.url_repository import UrlRepository

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
    async def test_increment_clicks_keeps_newest_last_click(self):
        col = make_collection()
        col.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
        ts = datetime(2024, 6, 1, tzinfo=timezone.utc)
        await self._repo(col).increment_clicks(URL_OID, last_click_time=ts)
        col.update_one.assert_awaited_once_with(
            {"_id": URL_OID},
            {"$inc": {"total_clicks": 1}, "$max": {"last_click": ts}},
        )

    @pytest.mark.asyncio
    async def test_increment_clicks_uses_now_when_no_time(self):
        col = make_collection()
        col.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
        await self._repo(col).increment_clicks(URL_OID)
        args = col.update_one.call_args
        update_doc = args[0][1]
        assert isinstance(update_doc["$max"]["last_click"], datetime)

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
        from app.schemas.models.url import UrlV2Doc

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
        from app.repositories.url_repository import UrlRepository

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
    async def test_delete_many_retain_blocked_excludes_blocked(self):
        # Erasure mode: BLOCKED docs on the fqdn are the abuse audit trail —
        # the domain cascade must not delete what the owner-wide step retained.
        col = make_collection()
        col.delete_many = AsyncMock(return_value=MagicMock(deleted_count=3))
        count = await self._repo(col).delete_many_by_owner_and_domain(
            USER_OID, "links.acme.com", retain_blocked=True
        )
        assert count == 3
        query = col.delete_many.call_args[0][0]
        assert query == {
            "owner_id": USER_OID,
            "domain": "links.acme.com",
            "status": {"$ne": "BLOCKED"},
        }

    @pytest.mark.asyncio
    async def test_delete_many_refuses_missing_filters(self):
        repo = self._repo()
        with pytest.raises(ValueError):
            await repo.delete_many_by_owner_and_domain(None, "links.acme.com")
        with pytest.raises(ValueError):
            await repo.delete_many_by_owner_and_domain(USER_OID, "")


class TestUrlRepositoryBulkByIds:
    def _repo(self, col=None):
        from app.repositories.url_repository import UrlRepository

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
            patch("app.repositories.url_repository.log") as log_mock,
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
        from app.repositories.url_repository import UrlRepository

        col = AsyncMock()
        col.name = "urlsV2"
        cursor = MagicMock()
        cursor.to_list = AsyncMock(
            return_value=[{"_id": "a.evil.com", "urls": ["https://a.evil.com/x"]}]
        )
        col.aggregate = AsyncMock(return_value=cursor)

        result = await UrlRepository(col).list_active_hosts_by_registrable("evil.com")

        assert result == [("a.evil.com", "https://a.evil.com/x")]
        pipeline = col.aggregate.await_args.args[0]
        assert {"dest.registrable_domain": "evil.com"} in pipeline[0]["$match"]["$or"]
        assert {"dest.secondary_registrable": "evil.com"} in pipeline[0]["$match"][
            "$or"
        ]
        assert pipeline[0]["$match"]["status"] == "ACTIVE"

    @pytest.mark.asyncio
    async def test_list_recent_destination_hosts_awaits_aggregate(self):
        from datetime import datetime, timezone

        from app.repositories.url_repository import UrlRepository

        col = AsyncMock()
        col.name = "urlsV2"
        cursor = MagicMock()
        cursor.to_list = AsyncMock(
            return_value=[{"_id": "fresh.com", "urls": ["https://fresh.com/a"]}]
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
        from app.repositories.url_repository import UrlRepository

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
        from app.repositories.url_repository import UrlRepository

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
        from app.schemas.models.base import ANONYMOUS_OWNER_ID

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
        from app.repositories.url_repository import UrlRepository

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


class TestUrlRepositoryOwnerErasure:
    """iter_by_owner + delete_by_owner — the account-erasure pair."""

    def _repo(self, col=None):
        from app.repositories.url_repository import UrlRepository

        return UrlRepository(col or make_collection())

    @pytest.mark.asyncio
    async def test_delete_by_owner_deletes_across_domains_except_blocked(self):
        col = make_collection()
        result = MagicMock()
        result.deleted_count = 4
        col.delete_many = AsyncMock(return_value=result)
        count = await self._repo(col).delete_by_owner(USER_OID)
        # BLOCKED docs are the abuse audit trail + alias reservation — the
        # erasure bulk delete must never touch them.
        col.delete_many.assert_awaited_once_with(
            {"owner_id": USER_OID, "status": {"$ne": "BLOCKED"}}
        )
        assert count == 4

    @pytest.mark.asyncio
    async def test_delete_by_owner_refuses_anonymous_sentinel(self):
        from app.schemas.models.base import ANONYMOUS_OWNER_ID

        col = make_collection()
        with pytest.raises(ValueError):
            await self._repo(col).delete_by_owner(ANONYMOUS_OWNER_ID)
        col.delete_many.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_scrub_blocked_pii_unsets_only_creator_pii(self):
        col = make_collection()
        result = MagicMock()
        result.matched_count = 2
        col.update_many = AsyncMock(return_value=result)
        retained = await self._repo(col).scrub_blocked_owner_pii(USER_OID)
        col.update_many.assert_awaited_once_with(
            {"owner_id": USER_OID, "status": "BLOCKED"},
            {
                "$unset": {
                    "creation_ip": "",
                    "meta_tags.updated_ip": "",
                    "password": "",
                }
            },
        )
        # matched (not modified): an already-scrubbed doc is still retained.
        assert retained == 2

    @pytest.mark.asyncio
    async def test_scrub_blocked_pii_domain_scoped(self):
        col = make_collection()
        result = MagicMock()
        result.matched_count = 1
        col.update_many = AsyncMock(return_value=result)
        retained = await self._repo(col).scrub_blocked_owner_pii(
            USER_OID, domain="links.acme.com"
        )
        query = col.update_many.call_args[0][0]
        assert query == {
            "owner_id": USER_OID,
            "status": "BLOCKED",
            "domain": "links.acme.com",
        }
        assert retained == 1

    @pytest.mark.asyncio
    async def test_scrub_blocked_pii_refuses_anonymous_sentinel(self):
        from app.schemas.models.base import ANONYMOUS_OWNER_ID

        col = make_collection()
        with pytest.raises(ValueError):
            await self._repo(col).scrub_blocked_owner_pii(ANONYMOUS_OWNER_ID)
        col.update_many.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_iter_by_owner_streams_models(self):
        class _Cursor:
            def __init__(self, docs):
                self._docs = docs

            def __aiter__(self):
                return self._gen()

            async def _gen(self):
                for d in self._docs:
                    yield d

        col = make_collection()
        col.find = MagicMock(return_value=_Cursor([_url_v2_doc()]))
        docs = [d async for d in self._repo(col).iter_by_owner(USER_OID)]
        col.find.assert_called_once_with({"owner_id": USER_OID})
        assert [d.alias for d in docs] == ["abc1234"]

    @pytest.mark.asyncio
    async def test_iter_by_owner_refuses_anonymous_sentinel(self):
        from app.schemas.models.base import ANONYMOUS_OWNER_ID

        col = make_collection()
        with pytest.raises(ValueError):
            async for _ in self._repo(col).iter_by_owner(ANONYMOUS_OWNER_ID):
                pass
        col.find.assert_not_called()

    @pytest.mark.asyncio
    async def test_iter_by_owner_propagates_pymongo_error(self):
        class _DeadCursor:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise OperationFailure("cursor died mid-stream")

        col = make_collection()
        col.find = MagicMock(return_value=_DeadCursor())
        with pytest.raises(OperationFailure):
            async for _ in self._repo(col).iter_by_owner(USER_OID):
                pass


class TestSecondaryHostMatching:
    """A host verdict must reach links that hide the host in a geo rule."""

    def _col(self):
        col = make_collection()
        col.name = "urlsV2"
        return col

    @pytest.mark.asyncio
    async def test_block_matches_main_or_secondary_host(self):
        from app.repositories.url_repository import UrlRepository, dest_host_filter

        col = self._col()
        col.update_many = AsyncMock(return_value=MagicMock(modified_count=1))
        await UrlRepository(col).block_active_by_dest_host("hidden.example", reason="r")
        # First call flips ACTIVE links; the second records the cause on already-blocked ones.
        flt, _ops = col.update_many.await_args_list[0].args
        assert flt["$or"] == dest_host_filter("hidden.example")["$or"]
        assert {"dest.secondary_hosts": "hidden.example"} in flt["$or"]
        assert flt["status"] == "ACTIVE"

    @pytest.mark.asyncio
    async def test_listing_for_eviction_matches_either_field(self):
        from app.repositories.url_repository import UrlRepository

        col = self._col()
        cursor = MagicMock()
        cursor.limit = MagicMock(return_value=cursor)
        cursor.to_list = AsyncMock(return_value=[])
        col.find = MagicMock(return_value=cursor)
        await UrlRepository(col).list_by_dest_host_with_urls("hidden.example")
        flt = col.find.call_args.args[0]
        assert {"dest.secondary_hosts": "hidden.example"} in flt["$or"]

    @pytest.mark.asyncio
    async def test_recent_hosts_sweep_unwinds_secondary_hosts(self):
        from datetime import datetime, timezone

        from app.repositories.url_repository import UrlRepository

        col = self._col()
        cursor = MagicMock()
        cursor.to_list = AsyncMock(return_value=[])
        col.aggregate = AsyncMock(return_value=cursor)
        await UrlRepository(col).list_recent_destination_hosts(
            datetime(2026, 9, 1, tzinfo=timezone.utc)
        )
        pipeline = col.aggregate.await_args.args[0]
        stages = [next(iter(s)) for s in pipeline]
        assert stages == ["$match", "$project", "$unwind", "$group", "$limit"]
        assert pipeline[3]["$group"]["_id"] == "$hosts"


class TestBlockCausesPerHost:
    """A link blocked for two of its destinations reactivates only when the
    last cause is removed; a per-link block is never undone by a host
    unblock; blocks stamped before the field are host-of-long_url blocks."""

    def _repo(self):
        from app.repositories.url_repository import UrlRepository

        col = make_collection()
        col.name = "urlsV2"
        col.update_many = AsyncMock(return_value=MagicMock(modified_count=1))
        return UrlRepository(col), col

    @pytest.mark.asyncio
    async def test_host_block_stamps_its_cause_and_adds_to_already_blocked_links(self):
        repo, col = self._repo()
        await repo.block_active_by_dest_host("b.example", reason="r")
        flip, add_cause = col.update_many.await_args_list
        assert flip.args[1]["$set"]["blocked_hosts"] == ["b.example"]
        assert add_cause.args[0]["status"] == "BLOCKED"
        assert {"blocked_hosts": {"$type": "array"}} in add_cause.args[0]["$or"]
        union = add_cause.args[1][0]["$set"]["blocked_hosts"]["$setUnion"]
        assert union[1] == ["b.example"]

    @pytest.mark.asyncio
    async def test_unblock_pulls_the_cause_then_reactivates_only_cause_free_links(self):
        repo, col = self._repo()
        await repo.unblock_by_dest_host("a.example")
        pull, flip = col.update_many.await_args_list
        assert pull.args == (
            {"blocked_hosts": "a.example", "status": "BLOCKED"},
            {"$pull": {"blocked_hosts": "a.example"}},
        )
        branches = flip.args[0]["$or"]
        assert branches[0]["blocked_hosts"] == []
        assert {"dest.host": "a.example"} in branches[0]["$or"]
        assert branches[1] == {
            "blocked_hosts": {"$exists": False},
            "dest.host": "a.example",
        }
        assert flip.args[1]["$set"]["status"] == "ACTIVE"

    @pytest.mark.asyncio
    async def test_per_link_block_carries_no_host_cause(self):
        repo, col = self._repo()
        await repo.block_active_by_aliases([("abc", "spoo.me")], reason="r")
        assert col.update_many.await_args.args[1]["$set"]["blocked_hosts"] is None

    @pytest.mark.asyncio
    async def test_second_cause_is_recorded_on_legacy_blocks_too(self):
        """A link blocked before blocked_hosts existed must not come back when
        its long_url host is unblocked while a geo host stays blocked."""
        repo, col = self._repo()
        await repo.block_active_by_dest_host("b.example", reason="r")
        _flip, add_cause = col.update_many.await_args_list
        flt, update = add_cause.args
        assert {"blocked_hosts": {"$exists": False}} in flt["$or"]
        assert flt["blocked_reason"] == {"$exists": True}
        union = update[0]["$set"]["blocked_hosts"]["$setUnion"]
        # Missing list: seed it with dest.host (the original cause), then add ours.
        assert union[0]["$ifNull"][0] == "$blocked_hosts"
        assert union[0]["$ifNull"][1]["$cond"][1] == ["$dest.host"]
        assert union[1] == ["b.example"]


class TestFeedSweepReachesSecondaryDestinations:
    """A geo or variant host under a feed-listed domain, whose main host is
    elsewhere, must come out of the feed-delta fan-out as its own host."""

    @pytest.mark.asyncio
    async def test_pipeline_keeps_only_hosts_under_the_domain(self):
        from app.repositories.url_repository import UrlRepository

        col = make_collection()
        col.name = "urlsV2"
        cursor = MagicMock()
        cursor.to_list = AsyncMock(return_value=[])
        col.aggregate = AsyncMock(return_value=cursor)
        await UrlRepository(col).list_active_hosts_by_registrable("evil.com")
        pipeline = col.aggregate.await_args.args[0]
        stages = [next(iter(st)) for st in pipeline]
        assert stages == ["$match", "$project", "$unwind", "$match", "$group", "$limit"]
        # Every (host, registrable) pair is unwound, then only pairs under the
        # feed domain survive, so the clean main host never gets enqueued.
        assert pipeline[3]["$match"] == {"pairs.1": "evil.com"}
        assert pipeline[4]["$group"]["_id"] == {"$arrayElemAt": ["$pairs", 0]}
        zipped = pipeline[1]["$project"]["pairs"]["$concatArrays"][1]["$zip"]["inputs"]
        assert zipped[0] == {"$ifNull": ["$dest.secondary_hosts", []]}
        assert zipped[1] == {"$ifNull": ["$dest.secondary_registrable", []]}


class TestSweepSampleUrls:
    def test_secondary_host_samples_its_own_url_not_the_main_one(self):
        from app.repositories.url_repository import _host_samples

        rows = [
            {
                "_id": "kit.evil.com",
                "urls": ["https://clean.example/landing", "https://kit.evil.com/kit"],
            },
            {"_id": "clean.example", "urls": ["https://clean.example/landing"]},
            {"_id": "orphan.example", "urls": ["https://elsewhere.example/"]},
            {"_id": "", "urls": ["https://ignored.example/"]},
        ]
        assert _host_samples(rows) == [
            ("kit.evil.com", "https://kit.evil.com/kit"),
            ("clean.example", "https://clean.example/landing"),
            ("orphan.example", "https://elsewhere.example/"),
        ]

    @pytest.mark.asyncio
    async def test_recent_hosts_projects_every_url_of_the_link(self):
        from datetime import datetime, timezone

        from app.repositories.url_repository import UrlRepository

        col = make_collection()
        col.name = "urlsV2"
        cursor = MagicMock()
        cursor.to_list = AsyncMock(return_value=[])
        col.aggregate = AsyncMock(return_value=cursor)
        await UrlRepository(col).list_recent_destination_hosts(
            datetime(2026, 9, 1, tzinfo=timezone.utc)
        )
        pipeline = col.aggregate.await_args.args[0]
        urls = pipeline[1]["$project"]["urls"]["$concatArrays"]
        assert urls[0] == ["$long_url"]
        assert urls[1]["$map"]["input"] == {
            "$objectToArray": {"$ifNull": ["$geo_rules", {}]}
        }
        assert urls[2]["$map"]["input"] == {"$ifNull": ["$ab_variants", []]}
        # One arm per single-URL destination field, in the shared list's order:
        # a field missing here is a sweep that samples the wrong URL.
        from app.shared.url_utils import SINGLE_DESTINATION_FIELDS

        arms = [arm["$cond"][1] for arm in urls[3:]]
        assert arms == [[f"${field}"] for field in SINGLE_DESTINATION_FIELDS]
        assert pipeline[3]["$group"]["urls"] == {"$first": "$urls"}
