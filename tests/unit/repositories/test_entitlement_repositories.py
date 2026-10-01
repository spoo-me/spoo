"""Subscription and override repositories: every effective write appends one
audit event and drops the owner's cache; a no-op write does neither."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from errors import ConflictError
from repositories.entitlement_override_repository import (
    EntitlementOverrideRepository,
)
from repositories.subscription_repository import SubscriptionRepository
from schemas.models.entitlement_event import EntitlementEventKind
from schemas.models.entitlement_override import OverrideKind
from schemas.models.subscription import (
    SubscriptionDoc,
    SubscriptionKind,
    SubscriptionProvider,
    SubscriptionStatus,
)
from services.features.catalog import validate_override

UID = ObjectId()
NOW = datetime(2026, 9, 4, tzinfo=timezone.utc)


def _col(name="subscriptions"):
    col = MagicMock()
    col.name = name
    col.insert_one = AsyncMock(return_value=MagicMock(inserted_id=ObjectId()))
    col.update_one = AsyncMock(
        return_value=MagicMock(matched_count=1, modified_count=1)
    )
    col.delete_one = AsyncMock(return_value=MagicMock(deleted_count=1))
    col.delete_many = AsyncMock(return_value=MagicMock(deleted_count=1))
    col.find_one = AsyncMock(return_value=None)
    return col


def _events_and_cache():
    events = AsyncMock()
    events.append = AsyncMock()
    cache = AsyncMock()
    cache.invalidate = AsyncMock()
    return events, cache


def _sub_raw(status="active", **extra):
    return {
        "_id": ObjectId(),
        "user_id": UID,
        "provider": "paddle",
        "kind": "recurring",
        "status": status,
        "current_period_end": NOW + timedelta(days=10),
        "created_at": NOW,
        "updated_at": NOW,
        **extra,
    }


class TestSubscriptionRepository:
    @pytest.mark.asyncio
    async def test_first_grant_inserts_and_records_subscription_changed(self):
        col = _col()
        events, cache = _events_and_cache()
        repo = SubscriptionRepository(col, events, cache)
        doc = await repo.write_transition(
            UID,
            before=None,
            after_status=SubscriptionStatus.ACTIVE,
            fields={
                "provider": SubscriptionProvider.PADDLE,
                "kind": SubscriptionKind.RECURRING,
                "current_period_end": NOW + timedelta(days=30),
            },
            actor="paddle",
            reason="subscription.activated",
            now=NOW,
        )
        assert doc.status is SubscriptionStatus.ACTIVE
        assert doc.id is not None
        col.insert_one.assert_awaited_once()
        event = events.append.await_args.args[0]
        assert event.kind is EntitlementEventKind.SUBSCRIPTION_CHANGED
        assert event.before is None
        assert event.after["status"] == "active"
        cache.invalidate.assert_awaited_once_with(UID)

    @pytest.mark.asyncio
    async def test_status_change_is_a_compare_and_set(self):
        col = _col()
        events, cache = _events_and_cache()
        repo = SubscriptionRepository(col, events, cache)
        col.find_one.return_value = _sub_raw("active")
        before = await repo.find_by_user(UID)
        await repo.write_transition(
            UID,
            before=before,
            after_status=SubscriptionStatus.PAST_DUE,
            fields={},
            actor="paddle",
            reason="payment failed",
            now=NOW,
        )
        query, ops = col.update_one.await_args.args
        assert query == {"user_id": UID, "status": "active"}
        assert ops["$set"]["status"] == "past_due"
        assert ops["$set"]["updated_at"] == NOW

    @pytest.mark.asyncio
    async def test_lost_cas_raises_conflict_without_event(self):
        col = _col()
        col.update_one.return_value = MagicMock(matched_count=0)
        events, cache = _events_and_cache()
        repo = SubscriptionRepository(col, events, cache)
        col.find_one.return_value = _sub_raw("active")
        before = await repo.find_by_user(UID)
        with pytest.raises(ConflictError):
            await repo.write_transition(
                UID,
                before=before,
                after_status=SubscriptionStatus.PAST_DUE,
                fields={},
                actor="paddle",
                reason="x",
            )
        events.append.assert_not_awaited()
        cache.invalidate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_duplicate_insert_raises_conflict(self):
        col = _col()
        col.insert_one.side_effect = DuplicateKeyError("dup")
        events, cache = _events_and_cache()
        repo = SubscriptionRepository(col, events, cache)
        with pytest.raises(ConflictError):
            await repo.write_transition(
                UID,
                before=None,
                after_status=SubscriptionStatus.ACTIVE,
                fields={
                    "provider": SubscriptionProvider.PADDLE,
                    "kind": SubscriptionKind.RECURRING,
                },
                actor="paddle",
                reason="x",
            )
        events.append.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unchanged_write_records_nothing(self):
        col = _col()
        events, cache = _events_and_cache()
        repo = SubscriptionRepository(col, events, cache)
        col.find_one.return_value = _sub_raw("active")
        before = await repo.find_by_user(UID)
        after = await repo.write_transition(
            UID,
            before=before,
            after_status=SubscriptionStatus.ACTIVE,
            fields={"current_period_end": before.current_period_end},
            actor="paddle",
            reason="duplicate delivery",
        )
        assert after is before
        col.update_one.assert_not_awaited()
        events.append.assert_not_awaited()
        cache.invalidate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returned_doc_is_validated_like_a_read(self):
        col = _col()
        events, cache = _events_and_cache()
        repo = SubscriptionRepository(col, events, cache)
        before = SubscriptionDoc.from_mongo(_sub_raw("active"))
        after = await repo.write_transition(
            UID,
            before=before,
            after_status=SubscriptionStatus.ACTIVE,
            fields={"kind": "prepaid", "prepaid_until": NOW + timedelta(days=365)},
            actor="paddle",
            reason="year purchased",
            now=NOW,
        )
        assert after.kind is SubscriptionKind.PREPAID
        assert after.term_end == NOW + timedelta(days=365)

    @pytest.mark.asyncio
    async def test_same_status_with_new_period_end_is_a_change(self):
        col = _col()
        events, cache = _events_and_cache()
        repo = SubscriptionRepository(col, events, cache)
        col.find_one.return_value = _sub_raw("active")
        before = await repo.find_by_user(UID)
        renewed = NOW + timedelta(days=40)
        after = await repo.write_transition(
            UID,
            before=before,
            after_status=SubscriptionStatus.ACTIVE,
            fields={"current_period_end": renewed},
            actor="paddle",
            reason="renewal",
        )
        assert after.current_period_end == renewed
        events.append.assert_awaited_once()
        cache.invalidate.assert_awaited_once_with(UID)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("after_status", "kind"),
        [
            (SubscriptionStatus.GRACE, EntitlementEventKind.GRACE_STARTED),
            (SubscriptionStatus.LAPSED, EntitlementEventKind.LAPSED),
            (SubscriptionStatus.PAST_DUE, EntitlementEventKind.SUBSCRIPTION_CHANGED),
        ],
    )
    async def test_event_kind_names_grace_and_lapse(self, after_status, kind):
        col = _col()
        events, cache = _events_and_cache()
        repo = SubscriptionRepository(col, events, cache)
        col.find_one.return_value = _sub_raw("active")
        before = await repo.find_by_user(UID)
        await repo.write_transition(
            UID,
            before=before,
            after_status=after_status,
            fields={"grace_until": NOW + timedelta(days=14)},
            actor="job",
            reason="clock",
        )
        assert events.append.await_args.args[0].kind is kind

    @pytest.mark.asyncio
    async def test_delete_by_user_invalidates_cache(self):
        col = _col()
        events, cache = _events_and_cache()
        repo = SubscriptionRepository(col, events, cache)
        assert await repo.delete_by_user(UID) == 1
        cache.invalidate.assert_awaited_once_with(UID)


class TestOverrideRepository:
    @pytest.mark.asyncio
    async def test_grant_upserts_records_event_and_invalidates(self):
        col = _col("entitlement_overrides")
        events, cache = _events_and_cache()
        repo = EntitlementOverrideRepository(col, events, cache)
        doc = await repo.grant(
            UID,
            "geo_targeting",
            True,
            kind=OverrideKind.BETA,
            reason="beta cohort",
            granted_by="ops:zingzy",
            now=NOW,
        )
        assert doc.value is True
        query, ops = col.update_one.await_args.args
        assert query == {"user_id": UID, "key": "geo_targeting"}
        assert ops["$set"]["kind"] == "beta"
        assert col.update_one.await_args.kwargs["upsert"] is True
        event = events.append.await_args.args[0]
        assert event.kind is EntitlementEventKind.OVERRIDE_GRANTED
        assert event.before is None
        assert event.after["value"] is True
        cache.invalidate.assert_awaited_once_with(UID)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("key", "value"),
        [("geo_targetting", True), ("geo_targeting", 5), ("custom_domains_max", True)],
        ids=["unknown_key", "int_for_bool", "bool_for_int"],
    )
    async def test_grant_rejects_unknown_key_or_wrong_type(self, key, value):
        col = _col("entitlement_overrides")
        events, cache = _events_and_cache()
        repo = EntitlementOverrideRepository(
            col, events, cache, check=validate_override
        )
        with pytest.raises(ValueError):
            await repo.grant(
                UID, key, value, kind=OverrideKind.COMP, reason="r", granted_by="ops"
            )
        col.update_one.assert_not_awaited()
        events.append.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_identical_regrant_is_a_noop(self):
        col = _col("entitlement_overrides")
        col.find_one.return_value = {
            "_id": ObjectId(),
            "user_id": UID,
            "key": "geo_targeting",
            "value": True,
            "kind": "beta",
            "reason": "beta cohort",
            "granted_by": "ops",
            "expires_at": None,
            "created_at": NOW,
            "updated_at": NOW,
        }
        events, cache = _events_and_cache()
        repo = EntitlementOverrideRepository(col, events, cache)
        await repo.grant(
            UID,
            "geo_targeting",
            True,
            kind=OverrideKind.BETA,
            reason="again",
            granted_by="ops",
        )
        col.update_one.assert_not_awaited()
        events.append.assert_not_awaited()
        cache.invalidate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_changed_value_regrant_records_before_and_after(self):
        col = _col("entitlement_overrides")
        col.find_one.return_value = {
            "_id": ObjectId(),
            "user_id": UID,
            "key": "custom_domains_max",
            "value": 3,
            "kind": "custom",
            "reason": "x",
            "granted_by": "ops",
            "expires_at": None,
        }
        events, cache = _events_and_cache()
        repo = EntitlementOverrideRepository(col, events, cache)
        await repo.grant(
            UID,
            "custom_domains_max",
            8,
            kind=OverrideKind.CUSTOM,
            reason="bigger",
            granted_by="ops",
        )
        event = events.append.await_args.args[0]
        assert event.before["value"] == 3
        assert event.after["value"] == 8

    @pytest.mark.asyncio
    async def test_revoke_deletes_records_and_invalidates(self):
        col = _col("entitlement_overrides")
        oid = ObjectId()
        col.find_one.return_value = {
            "_id": oid,
            "user_id": UID,
            "key": "geo_targeting",
            "value": True,
            "kind": "beta",
            "reason": "x",
            "granted_by": "ops",
        }
        events, cache = _events_and_cache()
        repo = EntitlementOverrideRepository(col, events, cache)
        assert await repo.revoke(UID, "geo_targeting", actor="ops", reason="done")
        col.delete_one.assert_awaited_once_with({"_id": oid})
        event = events.append.await_args.args[0]
        assert event.kind is EntitlementEventKind.OVERRIDE_REVOKED
        assert event.before["key"] == "geo_targeting"
        assert event.after is None
        cache.invalidate.assert_awaited_once_with(UID)

    @pytest.mark.asyncio
    async def test_revoke_of_missing_override_is_false_and_silent(self):
        col = _col("entitlement_overrides")
        events, cache = _events_and_cache()
        repo = EntitlementOverrideRepository(col, events, cache)
        assert await repo.revoke(UID, "geo_targeting", actor="ops", reason="x") is False
        events.append.assert_not_awaited()
        cache.invalidate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_list_active_filters_expired(self):
        col = _col("entitlement_overrides")
        cursor = MagicMock()
        cursor.to_list = AsyncMock(return_value=[])
        col.find = MagicMock(return_value=cursor)
        events, cache = _events_and_cache()
        repo = EntitlementOverrideRepository(col, events, cache)
        await repo.list_active(UID, NOW)
        query = col.find.call_args.args[0]
        assert query["user_id"] == UID
        assert {"expires_at": None} in query["$or"]
        assert {"expires_at": {"$gt": NOW}} in query["$or"]
