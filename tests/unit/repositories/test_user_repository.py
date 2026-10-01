"""Unit tests for UserRepository."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from pymongo.errors import (
    DuplicateKeyError,
    OperationFailure,
    ServerSelectionTimeoutError,
)

from .conftest import USER_OID, make_collection


class TestUserRepository:
    def _repo(self, col=None):
        from repositories.user_repository import UserRepository

        return UserRepository(col or make_collection())

    def _user_doc(self):
        return {
            "_id": USER_OID,
            "email": "test@example.com",
            "email_verified": True,
            "password_hash": None,
            "password_set": False,
            "auth_providers": [],
            "plan": "free",
            "status": "ACTIVE",
        }

    @pytest.mark.asyncio
    async def test_find_by_email_returns_model(self):
        col = make_collection()
        col.find_one = AsyncMock(return_value=self._user_doc())
        result = await self._repo(col).find_by_email("test@example.com")
        col.find_one.assert_awaited_once_with({"email": "test@example.com"})
        assert result is not None
        assert result.email == "test@example.com"

    @pytest.mark.asyncio
    async def test_find_by_email_returns_none_on_miss(self):
        col = make_collection()
        col.find_one = AsyncMock(return_value=None)
        assert await self._repo(col).find_by_email("nope@example.com") is None

    @pytest.mark.asyncio
    async def test_find_by_id_returns_model(self):
        col = make_collection()
        col.find_one = AsyncMock(return_value=self._user_doc())
        result = await self._repo(col).find_by_id(USER_OID)
        col.find_one.assert_awaited_once_with({"_id": USER_OID})
        assert result is not None

    @pytest.mark.asyncio
    async def test_find_by_id_returns_none_on_miss(self):
        col = make_collection()
        col.find_one = AsyncMock(return_value=None)
        assert await self._repo(col).find_by_id(USER_OID) is None

    @pytest.mark.asyncio
    async def test_find_by_oauth_provider(self):
        col = make_collection()
        col.find_one = AsyncMock(return_value=self._user_doc())
        result = await self._repo(col).find_by_oauth_provider(
            "google", "google-uid-123"
        )
        col.find_one.assert_awaited_once_with(
            {
                "auth_providers": {
                    "$elemMatch": {
                        "provider": "google",
                        "provider_user_id": "google-uid-123",
                    }
                }
            }
        )
        assert result is not None

    @pytest.mark.asyncio
    async def test_find_by_oauth_provider_returns_none(self):
        col = make_collection()
        col.find_one = AsyncMock(return_value=None)
        assert await self._repo(col).find_by_oauth_provider("github", "uid") is None

    @pytest.mark.asyncio
    async def test_create_returns_inserted_id(self):
        col = make_collection()
        mock_result = MagicMock(inserted_id=USER_OID)
        col.insert_one = AsyncMock(return_value=mock_result)
        user_data = {"email": "new@example.com"}
        oid = await self._repo(col).create(user_data)
        col.insert_one.assert_awaited_once_with(user_data)
        assert oid == USER_OID

    @pytest.mark.asyncio
    async def test_update_returns_true_on_match(self):
        col = make_collection()
        col.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
        ops = {"$set": {"email_verified": True}}
        ok = await self._repo(col).update(USER_OID, ops)
        col.update_one.assert_awaited_once_with({"_id": USER_OID}, ops)
        assert ok is True

    @pytest.mark.asyncio
    async def test_update_returns_false_on_no_match(self):
        col = make_collection()
        col.update_one = AsyncMock(return_value=MagicMock(matched_count=0))
        assert await self._repo(col).update(USER_OID, {"$set": {}}) is False

    @pytest.mark.asyncio
    async def test_raises_on_db_error(self):
        col = make_collection()
        col.find_one = AsyncMock(side_effect=RuntimeError("network error"))
        with pytest.raises(RuntimeError, match="network error"):
            await self._repo(col).find_by_email("test@example.com")

    # ── Error path tests ──────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_find_by_email_raises_on_operation_failure(self):
        """OperationFailure propagates from find_by_email."""
        col = make_collection()
        col.find_one = AsyncMock(side_effect=OperationFailure("query failed"))
        with pytest.raises(OperationFailure):
            await self._repo(col).find_by_email("test@example.com")

    @pytest.mark.asyncio
    async def test_find_by_id_raises_on_server_timeout(self):
        """ServerSelectionTimeoutError (MongoDB unreachable) propagates from find_by_id."""
        col = make_collection()
        col.find_one = AsyncMock(side_effect=ServerSelectionTimeoutError("timed out"))
        with pytest.raises(ServerSelectionTimeoutError):
            await self._repo(col).find_by_id(USER_OID)

    @pytest.mark.asyncio
    async def test_create_raises_duplicate_key(self):
        """DuplicateKeyError on create propagates (email unique index violation)."""
        col = make_collection()
        col.insert_one = AsyncMock(
            side_effect=DuplicateKeyError("E11000 duplicate key")
        )
        with pytest.raises(DuplicateKeyError):
            await self._repo(col).create({"email": "existing@example.com"})

    @pytest.mark.asyncio
    async def test_create_raises_on_operation_failure(self):
        """OperationFailure on create propagates."""
        col = make_collection()
        col.insert_one = AsyncMock(side_effect=OperationFailure("write failed"))
        with pytest.raises(OperationFailure):
            await self._repo(col).create({"email": "new@example.com"})

    @pytest.mark.asyncio
    async def test_update_raises_on_server_timeout(self):
        """ServerSelectionTimeoutError during update propagates."""
        col = make_collection()
        col.update_one = AsyncMock(side_effect=ServerSelectionTimeoutError("timed out"))
        with pytest.raises(ServerSelectionTimeoutError):
            await self._repo(col).update(USER_OID, {"$set": {"email_verified": True}})

    # ── Storage prefix pinning ────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_set_storage_prefix_if_absent_is_guarded_on_absence(self):
        col = make_collection()
        col.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
        ok = await self._repo(col).set_storage_prefix_if_absent(USER_OID, "abc123")
        assert ok is True
        query, ops = col.update_one.await_args.args
        # First-write-wins: only a doc without a pinned prefix matches.
        assert query == {"_id": USER_OID, "storage_prefix": None}
        assert ops == {"$set": {"storage_prefix": "abc123"}}

    @pytest.mark.asyncio
    async def test_set_storage_prefix_if_absent_noops_when_pinned(self):
        col = make_collection()
        col.update_one = AsyncMock(return_value=MagicMock(modified_count=0))
        ok = await self._repo(col).set_storage_prefix_if_absent(USER_OID, "abc123")
        assert ok is False

    # ── Pro tour ──────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_mark_pro_onboarded_is_first_wins(self):
        col = make_collection()
        col.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
        assert await self._repo(col).mark_pro_onboarded(USER_OID) is True
        query, ops = col.update_one.await_args.args
        assert query == {"_id": USER_OID, "pro_onboarded_at": None}
        assert set(ops) == {"$set"} and set(ops["$set"]) == {"pro_onboarded_at"}
        assert ops["$set"]["pro_onboarded_at"].tzinfo is not None

    @pytest.mark.asyncio
    async def test_mark_pro_onboarded_noops_once_stamped(self):
        col = make_collection()
        col.update_one = AsyncMock(return_value=MagicMock(modified_count=0))
        assert await self._repo(col).mark_pro_onboarded(USER_OID) is False

    # ── Pending deletion ──────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_mark_pending_deletion_sets_status_and_grace_window(self):
        col = make_collection()
        col.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
        before = datetime.now(timezone.utc)

        ok = await self._repo(col).mark_pending_deletion(USER_OID, grace_days=7)

        assert ok is True
        query, ops = col.update_one.await_args.args
        # Guarded transition: only an ACTIVE doc may flip.
        assert query == {"_id": USER_OID, "status": "ACTIVE"}
        st = ops["$set"]
        assert st["status"] == "PENDING_DELETION"
        assert st["deletion_requested_at"] >= before
        assert st["deletion_requested_at"].tzinfo is not None
        assert st["purge_after"] - st["deletion_requested_at"] == timedelta(days=7)

    @pytest.mark.asyncio
    async def test_mark_pending_deletion_zero_grace_purges_immediately(self):
        col = make_collection()
        col.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
        assert await self._repo(col).mark_pending_deletion(USER_OID, grace_days=0)
        _, ops = col.update_one.await_args.args
        st = ops["$set"]
        assert st["purge_after"] == st["deletion_requested_at"]

    @pytest.mark.asyncio
    async def test_mark_pending_deletion_rejected_when_not_active(self):
        """Already-pending (or INACTIVE) accounts don't match the filter."""
        col = make_collection()
        col.update_one = AsyncMock(return_value=MagicMock(modified_count=0))
        assert (
            await self._repo(col).mark_pending_deletion(USER_OID, grace_days=7) is False
        )

    @pytest.mark.asyncio
    async def test_restore_flips_pending_to_active_and_clears_fields(self):
        col = make_collection()
        col.update_one = AsyncMock(return_value=MagicMock(modified_count=1))

        ok = await self._repo(col).restore(USER_OID)

        assert ok is True
        query, ops = col.update_one.await_args.args
        # Guarded transition: only a PENDING_DELETION doc may flip back.
        assert query == {"_id": USER_OID, "status": "PENDING_DELETION"}
        assert ops["$set"] == {"status": "ACTIVE"}
        # erasure_claimed_at can only be set on ERASING docs (unreachable
        # from PENDING_DELETION) — unset anyway as claim-lease hygiene.
        assert set(ops["$unset"]) == {
            "deletion_requested_at",
            "purge_after",
            "erasure_claimed_at",
        }

    @pytest.mark.asyncio
    async def test_restore_rejected_when_not_pending(self):
        """ERASING (cascade claimed) and ACTIVE docs never match — once the
        claim lands, restore is refused for good."""
        col = make_collection()
        col.update_one = AsyncMock(return_value=MagicMock(modified_count=0))
        assert await self._repo(col).restore(USER_OID) is False

    # ── Erasure claim ─────────────────────────────────────────────────────────

    LEASE = 900

    def _purge_due_filter(self, now):
        """The $or both claim_for_erasure and find_purge_due must emit."""
        return [
            {"status": "PENDING_DELETION", "purge_after": {"$lte": now}},
            {
                "status": "ERASING",
                "erasure_claimed_at": {"$lte": now - timedelta(seconds=self.LEASE)},
            },
        ]

    @staticmethod
    def _matches(query: dict, doc: dict) -> bool:
        """Minimal Mongo-filter evaluator for the operators the claim uses
        ($or, equality, $lte) — proves match semantics, not just shape."""

        def field_matches(value, cond):
            if isinstance(cond, dict) and "$lte" in cond:
                return value is not None and value <= cond["$lte"]
            return value == cond

        for key, cond in query.items():
            if key == "$or":
                if not any(TestUserRepository._matches(c, doc) for c in cond):
                    return False
            elif not field_matches(doc.get(key), cond):
                return False
        return True

    @pytest.mark.asyncio
    async def test_claim_for_erasure_query_shape_and_success(self):
        """The claim matches purge-due PENDING_DELETION docs and
        expired-claim ERASING docs, flips to ERASING, and stamps the claim
        lease — without ever touching purge_after."""
        col = make_collection()
        col.update_one = AsyncMock(
            return_value=MagicMock(matched_count=1, modified_count=1)
        )
        now = datetime.now(timezone.utc)

        ok = await self._repo(col).claim_for_erasure(
            USER_OID, now=now, lease_seconds=self.LEASE
        )

        assert ok is True
        query, ops = col.update_one.await_args.args
        assert query == {"_id": USER_OID, "$or": self._purge_due_filter(now)}
        assert ops == {"$set": {"status": "ERASING", "erasure_claimed_at": now}}
        # purge_after survives the claim — a crashed cascade must stay
        # re-claimable on the next sweep.
        assert "$unset" not in ops

    @pytest.mark.asyncio
    async def test_claim_for_erasure_rejected_when_not_due_or_wrong_status(self):
        """Restored-to-ACTIVE, not-yet-due, and already-purged docs all
        fail the guarded filter — the account survives untouched."""
        col = make_collection()
        col.update_one = AsyncMock(
            return_value=MagicMock(matched_count=0, modified_count=0)
        )
        ok = await self._repo(col).claim_for_erasure(
            USER_OID, now=datetime.now(timezone.utc), lease_seconds=self.LEASE
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_claim_for_erasure_rejects_live_cascade(self):
        """THE lease's whole point: an ERASING doc with a fresh
        erasure_claimed_at is a cascade still running — a second runner's
        claim filter must not match it, even though it is purge-due."""
        col = make_collection()
        col.update_one = AsyncMock(
            return_value=MagicMock(matched_count=1, modified_count=1)
        )
        now = datetime.now(timezone.utc)
        await self._repo(col).claim_for_erasure(
            USER_OID, now=now, lease_seconds=self.LEASE
        )
        query, _ops = col.update_one.await_args.args

        live = {
            "_id": USER_OID,
            "status": "ERASING",
            "purge_after": now - timedelta(days=1),
            # Claimed mid-lease — e.g. 500s into a 600s scheduler lease.
            "erasure_claimed_at": now - timedelta(seconds=500),
        }
        assert self._matches(query, live) is False

    @pytest.mark.asyncio
    async def test_claim_for_erasure_reclaims_expired_claim(self):
        """A crashed cascade (claim older than the lease) is re-claimable;
        a legacy ERASING doc without the stamp is not silently matched."""
        col = make_collection()
        col.update_one = AsyncMock(
            return_value=MagicMock(matched_count=1, modified_count=1)
        )
        now = datetime.now(timezone.utc)
        ok = await self._repo(col).claim_for_erasure(
            USER_OID, now=now, lease_seconds=self.LEASE
        )
        assert ok is True
        query, _ops = col.update_one.await_args.args

        crashed = {
            "_id": USER_OID,
            "status": "ERASING",
            "purge_after": now - timedelta(days=1),
            "erasure_claimed_at": now - timedelta(seconds=self.LEASE + 1),
        }
        assert self._matches(query, crashed) is True

    @pytest.mark.asyncio
    async def test_claim_for_erasure_matches_due_pending_and_stamps_claim(self):
        """A due PENDING_DELETION doc claims regardless of the lease, and
        the winning update stamps erasure_claimed_at = now."""
        col = make_collection()
        col.update_one = AsyncMock(
            return_value=MagicMock(matched_count=1, modified_count=1)
        )
        now = datetime.now(timezone.utc)
        await self._repo(col).claim_for_erasure(
            USER_OID, now=now, lease_seconds=self.LEASE
        )
        query, ops = col.update_one.await_args.args

        due = {
            "_id": USER_OID,
            "status": "PENDING_DELETION",
            "purge_after": now - timedelta(seconds=1),
        }
        not_due = due | {"purge_after": now + timedelta(days=1)}
        assert self._matches(query, due) is True
        assert self._matches(query, not_due) is False
        assert ops["$set"]["erasure_claimed_at"] == now

    @pytest.mark.asyncio
    async def test_find_purge_due_query_shape_and_models(self):
        """Same $or as the claim — due PENDING_DELETION plus expired-claim
        ERASING; oldest deadline first, capped."""
        col = make_collection()
        now = datetime.now(timezone.utc)
        doc = self._user_doc() | {
            "status": "PENDING_DELETION",
            "deletion_requested_at": now - timedelta(days=7),
            "purge_after": now - timedelta(seconds=1),
        }
        cursor = col.find.return_value
        cursor.to_list = AsyncMock(return_value=[doc])

        due = await self._repo(col).find_purge_due(
            now=now, limit=25, lease_seconds=self.LEASE
        )

        col.find.assert_called_once_with({"$or": self._purge_due_filter(now)})
        cursor.sort.assert_called_once_with("purge_after", 1)
        cursor.limit.assert_called_once_with(25)
        cursor.to_list.assert_awaited_once_with(length=25)
        assert [u.id for u in due] == [USER_OID]
        assert due[0].purge_after == doc["purge_after"]

    @pytest.mark.asyncio
    async def test_find_purge_due_excludes_in_flight_erasing(self):
        """The sweep's view mirrors the claim: an in-flight cascade (fresh
        claim) stays out; a crashed one (expired claim) comes back."""
        col = make_collection()
        now = datetime.now(timezone.utc)
        cursor = col.find.return_value
        cursor.to_list = AsyncMock(return_value=[])

        await self._repo(col).find_purge_due(
            now=now, limit=25, lease_seconds=self.LEASE
        )
        (query,) = col.find.call_args.args

        base = {
            "status": "ERASING",
            "purge_after": now - timedelta(days=1),
        }
        in_flight = base | {"erasure_claimed_at": now - timedelta(seconds=10)}
        crashed = base | {"erasure_claimed_at": now - timedelta(seconds=self.LEASE + 1)}
        assert self._matches(query, in_flight) is False
        assert self._matches(query, crashed) is True

    @pytest.mark.asyncio
    async def test_find_purge_due_empty(self):
        col = make_collection()
        assert (
            await self._repo(col).find_purge_due(
                now=datetime.now(timezone.utc), limit=25, lease_seconds=self.LEASE
            )
            == []
        )

    @pytest.mark.asyncio
    async def test_find_purge_due_propagates_pymongo_error(self):
        col = make_collection()
        col.find.return_value.to_list = AsyncMock(
            side_effect=OperationFailure("conn lost")
        )
        with pytest.raises(OperationFailure):
            await self._repo(col).find_purge_due(
                now=datetime.now(timezone.utc), limit=25, lease_seconds=self.LEASE
            )

    @pytest.mark.asyncio
    async def test_delete_hard_removes_only_erasing_doc(self):
        col = make_collection()
        col.delete_one = AsyncMock(return_value=MagicMock(deleted_count=1))
        assert await self._repo(col).delete_hard(USER_OID) is True
        col.delete_one.assert_awaited_once_with({"_id": USER_OID, "status": "ERASING"})

    @pytest.mark.asyncio
    async def test_delete_hard_no_ops_on_non_erasing_doc(self):
        """A doc the cascade never claimed (ACTIVE, PENDING_DELETION) never
        matches the guarded filter — no stray hard delete."""
        col = make_collection()
        col.delete_one = AsyncMock(return_value=MagicMock(deleted_count=0))
        assert await self._repo(col).delete_hard(USER_OID) is False
