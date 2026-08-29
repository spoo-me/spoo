"""Unit tests for ensure_indexes."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pymongo.errors import CollectionInvalid, OperationFailure


class TestEnsureIndexes:
    @pytest.mark.asyncio
    async def test_ensure_indexes_calls_create_index(self):
        from repositories.indexes import ensure_indexes

        # Build a mock db with mock collections
        db = MagicMock()
        users_col = AsyncMock()
        urls_v2_col = AsyncMock()
        clicks_col = AsyncMock()
        api_keys_col = AsyncMock()
        tokens_col = AsyncMock()

        page_layouts_col = AsyncMock()
        urls_legacy_col = AsyncMock()
        emojis_col = AsyncMock()
        app_grants_col = AsyncMock()
        feature_flags_col = AsyncMock()
        custom_domains_col = AsyncMock()
        reports_col = AsyncMock()
        report_submissions_col = AsyncMock()
        webhook_events_col = AsyncMock()
        webhook_endpoints_col = AsyncMock()
        webhook_deliveries_col = AsyncMock()
        safety_verdicts_col = AsyncMock()
        feed_domains_col = AsyncMock()
        scheduled_tasks_col = AsyncMock()

        db.__getitem__ = lambda self, name: {
            "users": users_col,
            "urlsV2": urls_v2_col,
            "urls": urls_legacy_col,
            "emojis": emojis_col,
            "clicks": clicks_col,
            "api-keys": api_keys_col,
            "verification-tokens": tokens_col,
            "page-layouts": page_layouts_col,
            "app-grants": app_grants_col,
            "feature_flags": feature_flags_col,
            "custom_domains": custom_domains_col,
            "reports": reports_col,
            "report_submissions": report_submissions_col,
            "webhook-events": webhook_events_col,
            "webhook-endpoints": webhook_endpoints_col,
            "webhook-deliveries": webhook_deliveries_col,
            "safety_verdicts": safety_verdicts_col,
            "safety_feed_domains": feed_domains_col,
            "scheduled_tasks": scheduled_tasks_col,
        }[name]

        # create_collection raises CollectionInvalid when collection already exists
        db.create_collection = AsyncMock(side_effect=CollectionInvalid("clicks"))

        await ensure_indexes(db)

        # Check a few critical indexes
        users_col.create_index.assert_any_await([("email", 1)], unique=True)
        urls_v2_col.create_index.assert_any_await(
            [("owner_id", 1)],
            name="owner_claimed",
            partialFilterExpression={"claimed_at": {"$exists": True}},
        )
        page_layouts_col.create_index.assert_any_await(
            [("user_id", 1), ("page", 1)], unique=True
        )
        # Per-domain alias namespace via compound unique. The legacy
        # ``alias_1`` global unique is dropped (see test below).
        urls_v2_col.create_index.assert_any_await(
            [("domain", 1), ("alias", 1)], unique=True
        )
        urls_v2_col.drop_index.assert_any_await("alias_1")
        # Webhooks: the claim index and the coupled TTL pair.
        webhook_deliveries_col.create_index.assert_any_await(
            [("status", 1), ("next_attempt_at", 1)], name="ix_claim"
        )
        webhook_deliveries_col.create_index.assert_any_await(
            [("webhook_id", 1)], unique=True
        )
        webhook_events_col.create_index.assert_any_await([("event_id", 1)], unique=True)
        webhook_events_col.create_index.assert_any_await(
            [("created_at", 1)], expireAfterSeconds=2_592_000, name="ttl_created_at"
        )
        webhook_deliveries_col.create_index.assert_any_await(
            [("created_at", 1)], expireAfterSeconds=2_592_000, name="ttl_created_at"
        )
        # Safety verdicts: one per destination host.
        safety_verdicts_col.create_index.assert_any_await([("host", 1)], unique=True)
        safety_verdicts_col.create_index.assert_any_await([("registrable_domain", 1)])
        feed_domains_col.create_index.assert_any_await([("feed", 1), ("synced_at", 1)])
        # Destination decomposition: sparse dest_registrable on all three
        # url collections (pre-backfill docs lack `dest`).
        for _c in (urls_v2_col, urls_legacy_col, emojis_col):
            _c.create_index.assert_any_await(
                [("dest.registrable_domain", 1)], name="dest_registrable", sparse=True
            )
        # Scheduler: the task runner's claim index.
        scheduled_tasks_col.create_index.assert_any_await(
            [("enabled", 1), ("next_run_at", 1)], name="ix_claim"
        )
        urls_v2_col.create_index.assert_any_await([("owner_id", 1)])
        clicks_col.create_index.assert_any_await(
            [("meta.url_id", 1), ("clicked_at", -1)]
        )
        clicks_col.create_index.assert_any_await(
            [("meta.owner_id", 1), ("clicked_at", -1)]
        )
        clicks_col.create_index.assert_any_await(
            [("meta.domain", 1), ("clicked_at", -1)], sparse=True
        )
        api_keys_col.create_index.assert_any_await([("token_hash", 1)], unique=True)
        tokens_col.create_index.assert_any_await(
            [("expires_at", 1)], expireAfterSeconds=0
        )
        app_grants_col.create_index.assert_any_await(
            [("user_id", 1), ("app_id", 1)], unique=True
        )
        app_grants_col.create_index.assert_any_await(
            [("user_id", 1), ("revoked_at", 1)]
        )
        app_grants_col.create_index.assert_any_await([("app_id", 1), ("revoked_at", 1)])
        feature_flags_col.create_index.assert_any_await([("name", 1)], unique=True)
        # Reports: dedupe+velocity storage keys on the (domain, code) pair —
        # domain null for the system default. Secondary indexes serve the
        # funnel's triage reads (velocity sort, status filter).
        reports_col.create_index.assert_any_await(
            [("domain", 1), ("code", 1)], unique=True
        )
        reports_col.create_index.assert_any_await([("last_reported_at", -1)])
        reports_col.create_index.assert_any_await([("status", 1)])
        report_submissions_col.create_index.assert_any_await([("created_at", -1)])
        custom_domains_col.create_index.assert_any_await(
            [("fqdn", 1)],
            unique=True,
            partialFilterExpression={
                "status": {"$in": ["pending", "verifying", "active", "suspended"]}
            },
            name="fqdn_unique_non_revoked",
        )
        custom_domains_col.create_index.assert_any_await(
            [("owner_id", 1), ("created_at", -1)]
        )
        custom_domains_col.create_index.assert_any_await(
            [("status", 1), ("last_verified_at", 1)]
        )

    @pytest.mark.asyncio
    async def test_ensure_indexes_creates_timeseries_collection(self):
        from repositories.indexes import ensure_indexes

        db = MagicMock()
        for_col = AsyncMock()
        db.__getitem__ = lambda self, name: for_col
        db.create_collection = AsyncMock(return_value=None)

        await ensure_indexes(db)

        db.create_collection.assert_awaited_once_with(
            "clicks",
            timeseries={
                "timeField": "clicked_at",
                "metaField": "meta",
                "granularity": "seconds",
            },
        )

    @pytest.mark.asyncio
    async def test_drop_alias_1_swallows_index_not_found(self):
        # On boots after the legacy alias_1 has been dropped, drop_index
        # raises OperationFailure code 27. Must be silently swallowed so
        # ensure_indexes stays idempotent.
        from repositories.indexes import ensure_indexes

        db = MagicMock()
        col = AsyncMock()
        not_found = OperationFailure("alias_1 not found", code=27)
        col.drop_index = AsyncMock(side_effect=not_found)
        db.__getitem__ = lambda self, name: col
        db.create_collection = AsyncMock(side_effect=CollectionInvalid("clicks"))

        # Must not raise.
        await ensure_indexes(db)
        col.drop_index.assert_any_await("alias_1")

    @pytest.mark.asyncio
    async def test_drop_alias_1_propagates_other_errors(self):
        # Any drop_index failure that ISN'T IndexNotFound must propagate —
        # silent swallowing of e.g. permission errors would mask real bugs.
        from repositories.indexes import ensure_indexes

        db = MagicMock()
        col = AsyncMock()
        perm_err = OperationFailure("not authorized", code=13)  # Unauthorized
        col.drop_index = AsyncMock(side_effect=perm_err)
        db.__getitem__ = lambda self, name: col
        db.create_collection = AsyncMock(side_effect=CollectionInvalid("clicks"))

        with pytest.raises(OperationFailure):
            await ensure_indexes(db)
