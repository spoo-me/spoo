"""Unit tests for BulkUrlService — behavior and single-item parity.

The parity classes at the bottom are the anti-drift contract promised in
services/bulk_url_service.py: each scenario drives the REAL single-item
UrlService path and the bulk twin with identically-mocked dependencies
and asserts they agree (or asserts the documented divergence, where one
exists). If a change to either side breaks these, update both sides —
never just the assertion.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId
from pymongo.errors import PyMongoError

from errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from schemas.dto.requests.url import UpdateUrlRequest
from schemas.models.url import UrlStatus
from services.bulk_url_service import BulkBatch, BulkUrlService

from .test_url_service import (
    SYSTEM_DEFAULT_DOMAIN,
    USER_OID,
    make_url_v2_doc,
)

OTHER_OID = ObjectId("cccccccccccccccccccccccc")
FUTURE = datetime.now(timezone.utc) + timedelta(days=30)


def _oid(n: int) -> ObjectId:
    return ObjectId(f"{n:024x}")


def make_bulk_service(
    url_repo=None, url_cache=None, kv=None, url_service=None, events=None
) -> BulkUrlService:
    # The default url_service mock answers "available" for every alias
    # (AsyncMock truthiness); move tests override check_alias_available.
    return BulkUrlService(
        url_repo or AsyncMock(),
        url_cache or AsyncMock(),
        url_service=url_service or AsyncMock(),
        kv=kv,
        system_default_domain=SYSTEM_DEFAULT_DOMAIN,
        og_ttl_seconds=86_400,
        events=events,
    )


def _make_gate():
    from services.safety.policy import UrlPolicyService

    return UrlPolicyService([], blocked_self_domains=[SYSTEM_DEFAULT_DOMAIN])


def make_kv() -> MagicMock:
    kv = MagicMock()
    kv.bulk_delete = AsyncMock(return_value=True)
    kv.bulk_put = AsyncMock(return_value=True)
    return kv


async def drain_edge_tasks(svc: BulkUrlService) -> None:
    """The edge flush is deliberately detached; settle it before asserting."""
    while svc._inflight:
        await asyncio.gather(*list(svc._inflight))


# ─────────────────────────────────────────────────────────────────────────────
# BulkBatch
# ─────────────────────────────────────────────────────────────────────────────


class TestBulkBatch:
    def test_report_raises_on_missing_verdict(self):
        """Non-negotiable: no id may be silently skipped — structurally."""
        batch = BulkBatch([_oid(1), _oid(2)], {})
        batch.reject(_oid(1), "not_found", "URL not found")
        with pytest.raises(RuntimeError, match="missing verdicts"):
            batch.report(op="delete", user_id=USER_OID)

    def test_summary_derived_from_rows_in_request_order(self):
        batch = BulkBatch([_oid(1), _oid(2), _oid(3)], {})
        batch.ok(_oid(1), alias="a")
        batch.reject(_oid(2), "not_found", "URL not found")
        batch.ok(_oid(3), alias="c")
        report = batch.report(op="delete", user_id=USER_OID)
        assert (report.summary.total, report.summary.succeeded) == (3, 2)
        assert report.summary.failed == 1  # not_found counts as failed
        assert [row.id for row in report.results] == [
            str(_oid(1)),
            str(_oid(2)),
            str(_oid(3)),
        ]
        assert report.results[1].error_code == "not_found"
        assert report.results[1].alias is None


# ─────────────────────────────────────────────────────────────────────────────
# Load stage (shared)
# ─────────────────────────────────────────────────────────────────────────────


class TestBulkLoad:
    @pytest.mark.asyncio
    async def test_duplicates_dedupe_first_wins_and_total_counts_unique(self):
        doc = make_url_v2_doc(url_id=_oid(1))
        url_repo = AsyncMock()
        url_repo.find_by_ids_and_owner.return_value = [doc]
        url_repo.delete_by_ids_and_owner.return_value = 1
        svc = make_bulk_service(url_repo)

        report = await svc.bulk_delete([_oid(1), _oid(1), _oid(1)], USER_OID)

        assert report.summary.total == 1
        # The fetch itself sees the deduped list.
        fetched_ids = url_repo.find_by_ids_and_owner.call_args[0][0]
        assert fetched_ids == [_oid(1)]

    @pytest.mark.asyncio
    async def test_missing_and_foreign_ids_both_answer_not_found(self):
        """Ownership lives IN the query — the repo simply doesn't return
        foreign docs, so there is no existence oracle to probe."""
        url_repo = AsyncMock()
        url_repo.find_by_ids_and_owner.return_value = []
        svc = make_bulk_service(url_repo)

        report = await svc.bulk_delete([_oid(1), _oid(2)], USER_OID)

        assert report.summary.failed == 2
        assert {row.error_code for row in report.results} == {"not_found"}
        url_repo.delete_by_ids_and_owner.assert_not_called()

    @pytest.mark.asyncio
    async def test_blocked_rejected_per_item_with_alias_echoed(self):
        blocked = make_url_v2_doc(url_id=_oid(1), status="BLOCKED", alias="badlink")
        url_repo = AsyncMock()
        url_repo.find_by_ids_and_owner.return_value = [blocked]
        svc = make_bulk_service(url_repo)

        report = await svc.bulk_delete([_oid(1)], USER_OID)

        row = report.results[0]
        assert (row.ok, row.error_code, row.alias) == (False, "forbidden", "badlink")
        assert row.error == "Cannot delete a blocked URL"
        url_repo.delete_by_ids_and_owner.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# bulk_delete
# ─────────────────────────────────────────────────────────────────────────────


class TestBulkDelete:
    @pytest.mark.asyncio
    async def test_mixed_batch_end_to_end(self):
        ok_sys = make_url_v2_doc(url_id=_oid(1), alias="one")
        ok_tenant = make_url_v2_doc(
            url_id=_oid(2), alias="two", domain="links.acme.com"
        )
        blocked = make_url_v2_doc(url_id=_oid(3), alias="three", status="BLOCKED")
        url_repo = AsyncMock()
        url_repo.find_by_ids_and_owner.return_value = [ok_sys, ok_tenant, blocked]
        url_repo.delete_by_ids_and_owner.return_value = 2
        url_cache = AsyncMock()
        kv = make_kv()
        svc = make_bulk_service(url_repo, url_cache, kv)

        report = await svc.bulk_delete([_oid(1), _oid(2), _oid(3), _oid(4)], USER_OID)
        await drain_edge_tasks(svc)

        assert (report.summary.total, report.summary.succeeded) == (4, 2)
        by_id = {row.id: row for row in report.results}
        assert by_id[str(_oid(1))].ok and by_id[str(_oid(2))].ok
        assert by_id[str(_oid(3))].error_code == "forbidden"
        assert by_id[str(_oid(4))].error_code == "not_found"
        # Write hit exactly the deletable slice, ownership included.
        url_repo.delete_by_ids_and_owner.assert_awaited_once_with(
            [_oid(1), _oid(2)], USER_OID
        )
        # Redis grouped per domain.
        invalidated = {
            (tuple(call.args[0]), call.args[1])
            for call in url_cache.invalidate_many.await_args_list
        }
        assert invalidated == {
            (("one",), SYSTEM_DEFAULT_DOMAIN),
            (("two",), "links.acme.com"),
        }
        # Edge purge: system-domain keys only (tenants have no KV entries),
        # one bulk call.
        kv.bulk_delete.assert_awaited_once_with([f"cache:{SYSTEM_DEFAULT_DOMAIN}:one"])
        kv.bulk_put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_all_items_failing_still_answers_200_shape(self):
        url_repo = AsyncMock()
        url_repo.find_by_ids_and_owner.return_value = []
        svc = make_bulk_service(url_repo)
        report = await svc.bulk_delete([_oid(1)], USER_OID)
        assert report.summary.succeeded == 0
        assert report.results[0].ok is False

    @pytest.mark.asyncio
    async def test_write_failure_recovery_attributes_exact_truth(self):
        """delete_many threw mid-flight: the re-query decides — gone ids
        report ok (with side effects), survivors report internal."""
        gone = make_url_v2_doc(url_id=_oid(1), alias="gone")
        survivor = make_url_v2_doc(url_id=_oid(2), alias="stuck")
        url_repo = AsyncMock()
        url_repo.find_by_ids_and_owner.side_effect = [
            [gone, survivor],  # the load
            [survivor],  # the recovery re-query
        ]
        url_repo.delete_by_ids_and_owner.side_effect = PyMongoError("boom")
        url_cache = AsyncMock()
        kv = make_kv()
        svc = make_bulk_service(url_repo, url_cache, kv)

        report = await svc.bulk_delete([_oid(1), _oid(2)], USER_OID)
        await drain_edge_tasks(svc)

        by_id = {row.id: row for row in report.results}
        assert by_id[str(_oid(1))].ok is True
        assert by_id[str(_oid(2))].error_code == "internal"
        # The confirmed-gone item still got its cache + edge cleanup.
        url_cache.invalidate_many.assert_awaited_once_with(
            ["gone"], SYSTEM_DEFAULT_DOMAIN
        )
        kv.bulk_delete.assert_awaited_once_with([f"cache:{SYSTEM_DEFAULT_DOMAIN}:gone"])

    @pytest.mark.asyncio
    async def test_recovery_query_also_failing_reports_not_attempted(self):
        doc = make_url_v2_doc(url_id=_oid(1))
        url_repo = AsyncMock()
        url_repo.find_by_ids_and_owner.side_effect = [[doc], PyMongoError("down")]
        url_repo.delete_by_ids_and_owner.side_effect = PyMongoError("boom")
        svc = make_bulk_service(url_repo)

        report = await svc.bulk_delete([_oid(1)], USER_OID)

        assert report.results[0].error_code == "not_attempted"

    @pytest.mark.asyncio
    async def test_no_kv_client_skips_edge_flush(self):
        doc = make_url_v2_doc(url_id=_oid(1))
        url_repo = AsyncMock()
        url_repo.find_by_ids_and_owner.return_value = [doc]
        url_repo.delete_by_ids_and_owner.return_value = 1
        svc = make_bulk_service(url_repo, kv=None)

        report = await svc.bulk_delete([_oid(1)], USER_OID)

        assert report.summary.succeeded == 1
        assert not svc._inflight


# ─────────────────────────────────────────────────────────────────────────────
# bulk_set_status
# ─────────────────────────────────────────────────────────────────────────────


class TestBulkSetStatus:
    @pytest.mark.asyncio
    async def test_same_status_is_success_noop_without_write(self):
        """Single-item parity: the handler builds no ops, nothing is
        written, updated_at is NOT bumped."""
        active = make_url_v2_doc(url_id=_oid(1))
        url_repo = AsyncMock()
        url_repo.find_by_ids_and_owner.return_value = [active]
        svc = make_bulk_service(url_repo)

        report = await svc.bulk_set_status([_oid(1)], UrlStatus.ACTIVE, USER_OID)

        assert report.results[0].ok is True
        url_repo.update_by_ids_and_owner.assert_not_called()

    @pytest.mark.asyncio
    async def test_deactivate_writes_slice_and_purges_edge_keys(self):
        active_og = make_url_v2_doc(
            url_id=_oid(1), alias="og", meta_tags={"title": "T"}
        )
        active_plain = make_url_v2_doc(url_id=_oid(2), alias="plain")
        tenant = make_url_v2_doc(url_id=_oid(3), alias="ten", domain="links.acme.com")
        url_repo = AsyncMock()
        url_repo.find_by_ids_and_owner.return_value = [active_og, active_plain, tenant]
        url_repo.update_by_ids_and_owner.return_value = 3
        url_cache = AsyncMock()
        kv = make_kv()
        svc = make_bulk_service(url_repo, url_cache, kv)

        report = await svc.bulk_set_status(
            [_oid(1), _oid(2), _oid(3)], UrlStatus.INACTIVE, USER_OID
        )
        await drain_edge_tasks(svc)

        assert report.summary.succeeded == 3
        ids, owner, set_ops = url_repo.update_by_ids_and_owner.call_args[0]
        assert set(ids) == {_oid(1), _oid(2), _oid(3)}
        assert owner == USER_OID
        assert set_ops["status"] == UrlStatus.INACTIVE
        assert "updated_at" in set_ops
        # Takedown purge: og entry AND any hot-promoted entry (same key),
        # system-domain links only.
        kv.bulk_delete.assert_awaited_once()
        assert sorted(kv.bulk_delete.call_args[0][0]) == [
            f"cache:{SYSTEM_DEFAULT_DOMAIN}:og",
            f"cache:{SYSTEM_DEFAULT_DOMAIN}:plain",
        ]
        kv.bulk_put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_activate_reputs_og_entries_only(self):
        inactive_og = make_url_v2_doc(
            url_id=_oid(1), alias="og", status="INACTIVE", meta_tags={"title": "T"}
        )
        inactive_plain = make_url_v2_doc(
            url_id=_oid(2), alias="plain", status="INACTIVE"
        )
        expired = make_url_v2_doc(url_id=_oid(3), alias="exp", status="EXPIRED")
        url_repo = AsyncMock()
        url_repo.find_by_ids_and_owner.return_value = [
            inactive_og,
            inactive_plain,
            expired,
        ]
        url_repo.update_by_ids_and_owner.return_value = 3
        kv = make_kv()
        svc = make_bulk_service(url_repo, kv=kv)

        with patch(
            "services.bulk_url_service.build_og_entry",
            return_value=(f"cache:{SYSTEM_DEFAULT_DOMAIN}:og", "{}"),
        ) as build:
            report = await svc.bulk_set_status(
                [_oid(1), _oid(2), _oid(3)], UrlStatus.ACTIVE, USER_OID
            )
            await drain_edge_tasks(svc)

        # EXPIRED set to ACTIVE reactivates like the single-item route.
        assert report.summary.succeeded == 3
        # Only the og link gets a KV write, rendered from post-write state.
        build.assert_called_once()
        assert build.call_args[0][0].url_status == UrlStatus.ACTIVE
        kv.bulk_put.assert_awaited_once_with(
            [(f"cache:{SYSTEM_DEFAULT_DOMAIN}:og", "{}")], expiration_ttl=86_400
        )
        kv.bulk_delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_write_failure_marks_slice_internal_and_still_invalidates(self):
        doc = make_url_v2_doc(url_id=_oid(1), alias="a")
        url_repo = AsyncMock()
        url_repo.find_by_ids_and_owner.return_value = [doc]
        url_repo.update_by_ids_and_owner.side_effect = PyMongoError("boom")
        url_cache = AsyncMock()
        kv = make_kv()
        svc = make_bulk_service(url_repo, url_cache, kv)

        report = await svc.bulk_set_status([_oid(1)], UrlStatus.INACTIVE, USER_OID)
        await drain_edge_tasks(svc)

        assert report.results[0].error_code == "internal"
        # update_many may have partially applied — stale cache entries
        # serve lies, a spurious miss re-reads truth.
        url_cache.invalidate_many.assert_awaited_once_with(["a"], SYSTEM_DEFAULT_DOMAIN)
        kv.bulk_delete.assert_awaited_once()


# ─────────────────────────────────────────────────────────────────────────────
# bulk_set_expiry
# ─────────────────────────────────────────────────────────────────────────────


class TestBulkSetExpiry:
    @pytest.mark.asyncio
    async def test_past_value_rejects_envelope_before_any_fetch(self):
        url_repo = AsyncMock()
        svc = make_bulk_service(url_repo)
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        with pytest.raises(ValidationError, match="future"):
            await svc.bulk_set_expiry([_oid(1)], past, USER_OID)
        url_repo.find_by_ids_and_owner.assert_not_called()

    @pytest.mark.asyncio
    async def test_clear_is_noop_for_already_clear_links(self):
        clear = make_url_v2_doc(url_id=_oid(1))
        expiring = make_url_v2_doc(url_id=_oid(2), alias="exp", expire_after=FUTURE)
        url_repo = AsyncMock()
        url_repo.find_by_ids_and_owner.return_value = [clear, expiring]
        url_repo.update_by_ids_and_owner.return_value = 1
        svc = make_bulk_service(url_repo)

        report = await svc.bulk_set_expiry([_oid(1), _oid(2)], None, USER_OID)

        assert report.summary.succeeded == 2
        ids, _, set_ops = url_repo.update_by_ids_and_owner.call_args[0]
        assert ids == [_oid(2)]  # only the truthy-expiry link is written
        assert set_ops["expire_after"] is None

    @pytest.mark.asyncio
    async def test_same_value_is_noop(self):
        doc = make_url_v2_doc(url_id=_oid(1), expire_after=FUTURE)
        url_repo = AsyncMock()
        url_repo.find_by_ids_and_owner.return_value = [doc]
        svc = make_bulk_service(url_repo)

        report = await svc.bulk_set_expiry([_oid(1)], FUTURE, USER_OID)

        assert report.results[0].ok is True
        url_repo.update_by_ids_and_owner.assert_not_called()

    @pytest.mark.asyncio
    async def test_expired_links_reactivate_in_their_own_write(self):
        plain = make_url_v2_doc(url_id=_oid(1), alias="plain")
        expired_og = make_url_v2_doc(
            url_id=_oid(2), alias="dead", status="EXPIRED", meta_tags={"title": "T"}
        )
        url_repo = AsyncMock()
        url_repo.find_by_ids_and_owner.return_value = [plain, expired_og]
        url_repo.update_by_ids_and_owner.return_value = 1
        kv = make_kv()
        svc = make_bulk_service(url_repo, kv=kv)

        with patch(
            "services.bulk_url_service.build_og_entry",
            return_value=(f"cache:{SYSTEM_DEFAULT_DOMAIN}:dead", "{}"),
        ) as build:
            report = await svc.bulk_set_expiry([_oid(1), _oid(2)], FUTURE, USER_OID)
            await drain_edge_tasks(svc)

        assert report.summary.succeeded == 2
        writes = url_repo.update_by_ids_and_owner.await_args_list
        assert len(writes) == 2
        plain_ids, _, plain_ops = writes[0].args
        react_ids, _, react_ops = writes[1].args
        assert plain_ids == [_oid(1)] and "status" not in plain_ops
        assert react_ids == [_oid(2)] and react_ops["status"] == UrlStatus.ACTIVE
        assert react_ops["expire_after"] == FUTURE
        # The reactivated og link re-enters edge serving with ACTIVE state.
        build.assert_called_once()
        assert build.call_args[0][0].url_status == UrlStatus.ACTIVE
        kv.bulk_put.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_plain_slice_failure_does_not_sink_reactivate_slice(self):
        plain = make_url_v2_doc(url_id=_oid(1), alias="plain")
        expired = make_url_v2_doc(url_id=_oid(2), alias="dead", status="EXPIRED")
        url_repo = AsyncMock()
        url_repo.find_by_ids_and_owner.return_value = [plain, expired]
        url_repo.update_by_ids_and_owner.side_effect = [PyMongoError("boom"), 1]
        url_cache = AsyncMock()
        svc = make_bulk_service(url_repo, url_cache)

        report = await svc.bulk_set_expiry([_oid(1), _oid(2)], FUTURE, USER_OID)

        by_id = {row.id: row for row in report.results}
        assert by_id[str(_oid(1))].error_code == "internal"
        assert by_id[str(_oid(2))].ok is True


# ─────────────────────────────────────────────────────────────────────────────
# Parity with the single-item paths (the anti-drift contract)
# ─────────────────────────────────────────────────────────────────────────────


def make_single_item_service(url_repo, url_cache, og_writethrough=None, edge_kv=None):
    from services.url_service import UrlService

    return UrlService(
        url_repo=url_repo,
        legacy_repo=AsyncMock(),
        emoji_repo=AsyncMock(),
        blocked_url_repo=AsyncMock(),
        url_cache=url_cache,
        blocked_self_domains=[SYSTEM_DEFAULT_DOMAIN],
        system_default_domain=SYSTEM_DEFAULT_DOMAIN,
        url_policy=_make_gate(),
        og_writethrough=og_writethrough,
        edge_kv=edge_kv,
    )


class TestDeleteParity:
    """UrlService.delete vs bulk_delete-of-one, identical mocked deps."""

    @pytest.mark.asyncio
    async def test_owned_active_link_same_effects(self):
        doc = make_url_v2_doc(url_id=_oid(1), alias="promo")

        single_repo, single_cache = AsyncMock(), AsyncMock()
        single_repo.find_by_id.return_value = doc
        og = MagicMock(remove=AsyncMock(), sync=AsyncMock())
        single_kv = MagicMock()
        single_kv.delete = AsyncMock(return_value=True)
        single = make_single_item_service(single_repo, single_cache, og, single_kv)
        await single.delete(_oid(1), USER_OID)
        while single._edge_purge_tasks:
            await asyncio.gather(*list(single._edge_purge_tasks))

        bulk_repo, bulk_cache = AsyncMock(), AsyncMock()
        bulk_repo.find_by_ids_and_owner.return_value = [doc]
        bulk_repo.delete_by_ids_and_owner.return_value = 1
        kv = make_kv()
        bulk = make_bulk_service(bulk_repo, bulk_cache, kv)
        report = await bulk.bulk_delete([_oid(1)], USER_OID)
        await drain_edge_tasks(bulk)

        # Same doc removed, same Redis key cleared.
        single_repo.delete.assert_awaited_once_with(_oid(1))
        bulk_repo.delete_by_ids_and_owner.assert_awaited_once_with([_oid(1)], USER_OID)
        single_cache.invalidate.assert_awaited_once_with("promo", SYSTEM_DEFAULT_DOMAIN)
        bulk_cache.invalidate_many.assert_awaited_once_with(
            ["promo"], SYSTEM_DEFAULT_DOMAIN
        )
        assert report.results[0].ok is True
        # Plain link: BOTH paths drop the same edge key (takedown parity —
        # a deleted hot link stops serving at the edge regardless of which
        # route killed it). og.remove stays og-only on both sides.
        og.remove.assert_not_awaited()
        single_kv.delete.assert_awaited_once_with(
            f"cache:{SYSTEM_DEFAULT_DOMAIN}:promo"
        )
        kv.bulk_delete.assert_awaited_once_with(
            [f"cache:{SYSTEM_DEFAULT_DOMAIN}:promo"]
        )

    @pytest.mark.asyncio
    async def test_og_link_kv_key_matches_single_item_remove(self):
        doc = make_url_v2_doc(url_id=_oid(1), alias="og", meta_tags={"title": "T"})

        single_repo, single_cache = AsyncMock(), AsyncMock()
        single_repo.find_by_id.return_value = doc
        og = MagicMock(remove=AsyncMock(), sync=AsyncMock())
        single = make_single_item_service(single_repo, single_cache, og)
        await single.delete(_oid(1), USER_OID)
        og.remove.assert_awaited_once_with(SYSTEM_DEFAULT_DOMAIN, "og")

        bulk_repo = AsyncMock()
        bulk_repo.find_by_ids_and_owner.return_value = [doc]
        bulk_repo.delete_by_ids_and_owner.return_value = 1
        kv = make_kv()
        bulk = make_bulk_service(bulk_repo, kv=kv)
        await bulk.bulk_delete([_oid(1)], USER_OID)
        await drain_edge_tasks(bulk)

        # og.remove(domain, alias) deletes cache_key(domain, alias) — the
        # bulk purge hits the same key.
        kv.bulk_delete.assert_awaited_once_with([f"cache:{SYSTEM_DEFAULT_DOMAIN}:og"])

    @pytest.mark.asyncio
    async def test_blocked_and_missing_verdicts_match(self):
        blocked = make_url_v2_doc(url_id=_oid(1), status="BLOCKED")

        single_repo = AsyncMock()
        single = make_single_item_service(single_repo, AsyncMock())
        single_repo.find_by_id.return_value = blocked
        with pytest.raises(ForbiddenError, match="Cannot delete a blocked URL"):
            await single.delete(_oid(1), USER_OID)
        single_repo.find_by_id.return_value = None
        with pytest.raises(NotFoundError, match="URL not found"):
            await single.delete(_oid(2), USER_OID)

        bulk_repo = AsyncMock()
        bulk_repo.find_by_ids_and_owner.return_value = [blocked]
        bulk = make_bulk_service(bulk_repo)
        report = await bulk.bulk_delete([_oid(1), _oid(2)], USER_OID)

        by_id = {row.id: row for row in report.results}
        assert by_id[str(_oid(1))].error_code == "forbidden"
        assert by_id[str(_oid(1))].error == "Cannot delete a blocked URL"
        assert by_id[str(_oid(2))].error_code == "not_found"
        assert by_id[str(_oid(2))].error == "URL not found"

    @pytest.mark.asyncio
    async def test_foreign_id_divergence_is_the_documented_one(self):
        """Single-item answers forbidden for someone else's link; bulk
        answers not_found (ownership in the query, no existence oracle
        over foreign ObjectIds). The divergence is deliberate — this test
        pins it so it cannot drift silently; the rationale lives in the
        BulkUrlService module docstring."""
        foreign = make_url_v2_doc(url_id=_oid(1), owner_id=OTHER_OID)

        single_repo = AsyncMock()
        single_repo.find_by_id.return_value = foreign
        single = make_single_item_service(single_repo, AsyncMock())
        with pytest.raises(ForbiddenError):
            await single.delete(_oid(1), USER_OID)

        bulk_repo = AsyncMock()
        bulk_repo.find_by_ids_and_owner.return_value = []  # query excludes it
        bulk = make_bulk_service(bulk_repo)
        report = await bulk.bulk_delete([_oid(1)], USER_OID)
        assert report.results[0].error_code == "not_found"


class TestStatusParity:
    """UrlService.update(status-only) vs bulk_set_status-of-one."""

    @pytest.mark.asyncio
    async def test_status_change_writes_same_fields(self):
        doc = make_url_v2_doc(url_id=_oid(1), alias="promo")

        single_repo, single_cache = AsyncMock(), AsyncMock()
        single_repo.find_by_id.return_value = doc
        single = make_single_item_service(single_repo, single_cache)
        await single.update(
            _oid(1), UpdateUrlRequest(status=UrlStatus.INACTIVE), USER_OID
        )
        single_set = single_repo.update.call_args[0][1]["$set"]

        bulk_repo, bulk_cache = AsyncMock(), AsyncMock()
        bulk_repo.find_by_ids_and_owner.return_value = [doc]
        bulk_repo.update_by_ids_and_owner.return_value = 1
        bulk = make_bulk_service(bulk_repo, bulk_cache)
        await bulk.bulk_set_status([_oid(1)], UrlStatus.INACTIVE, USER_OID)
        _, _, bulk_set = bulk_repo.update_by_ids_and_owner.call_args[0]

        # Identical $set shape: {status, updated_at} and nothing else.
        assert (
            set(single_set.keys()) == set(bulk_set.keys()) == {"status", "updated_at"}
        )
        assert single_set["status"] == bulk_set["status"] == UrlStatus.INACTIVE
        single_cache.invalidate.assert_awaited_once_with("promo", SYSTEM_DEFAULT_DOMAIN)
        bulk_cache.invalidate_many.assert_awaited_once_with(
            ["promo"], SYSTEM_DEFAULT_DOMAIN
        )

    @pytest.mark.asyncio
    async def test_noop_writes_nothing_on_either_path(self):
        doc = make_url_v2_doc(url_id=_oid(1))

        single_repo = AsyncMock()
        single_repo.find_by_id.return_value = doc
        single = make_single_item_service(single_repo, AsyncMock())
        result = await single.update(
            _oid(1), UpdateUrlRequest(status=UrlStatus.ACTIVE), USER_OID
        )
        assert result is doc
        single_repo.update.assert_not_called()

        bulk_repo = AsyncMock()
        bulk_repo.find_by_ids_and_owner.return_value = [doc]
        bulk = make_bulk_service(bulk_repo)
        report = await bulk.bulk_set_status([_oid(1)], UrlStatus.ACTIVE, USER_OID)
        assert report.results[0].ok is True
        bulk_repo.update_by_ids_and_owner.assert_not_called()


class TestExpiryParity:
    """UrlService.update(expire_after-only) vs bulk_set_expiry-of-one."""

    @pytest.mark.asyncio
    async def test_setting_expiry_writes_same_fields(self):
        doc = make_url_v2_doc(url_id=_oid(1))

        single_repo = AsyncMock()
        single_repo.find_by_id.return_value = doc
        single = make_single_item_service(single_repo, AsyncMock())
        await single.update(_oid(1), UpdateUrlRequest(expire_after=FUTURE), USER_OID)
        single_set = single_repo.update.call_args[0][1]["$set"]

        bulk_repo = AsyncMock()
        bulk_repo.find_by_ids_and_owner.return_value = [doc]
        bulk_repo.update_by_ids_and_owner.return_value = 1
        bulk = make_bulk_service(bulk_repo)
        await bulk.bulk_set_expiry([_oid(1)], FUTURE, USER_OID)
        _, _, bulk_set = bulk_repo.update_by_ids_and_owner.call_args[0]

        assert (
            set(single_set.keys())
            == set(bulk_set.keys())
            == {
                "expire_after",
                "updated_at",
            }
        )
        assert single_set["expire_after"] == bulk_set["expire_after"] == FUTURE

    @pytest.mark.asyncio
    async def test_past_value_same_validation_error(self):
        doc = make_url_v2_doc(url_id=_oid(1))
        past = datetime.now(timezone.utc) - timedelta(hours=1)

        single_repo = AsyncMock()
        single_repo.find_by_id.return_value = doc
        single = make_single_item_service(single_repo, AsyncMock())
        with pytest.raises(ValidationError, match="expire_after must be in the future"):
            await single.update(_oid(1), UpdateUrlRequest(expire_after=past), USER_OID)

        bulk = make_bulk_service(AsyncMock())
        with pytest.raises(ValidationError, match="expire_after must be in the future"):
            await bulk.bulk_set_expiry([_oid(1)], past, USER_OID)

    @pytest.mark.asyncio
    async def test_expired_link_reactivates_on_both_paths(self):
        doc = make_url_v2_doc(url_id=_oid(1), status="EXPIRED")

        single_repo = AsyncMock()
        single_repo.find_by_id.return_value = doc
        single = make_single_item_service(single_repo, AsyncMock())
        await single.update(_oid(1), UpdateUrlRequest(expire_after=FUTURE), USER_OID)
        single_set = single_repo.update.call_args[0][1]["$set"]
        assert single_set["status"] == UrlStatus.ACTIVE  # _auto_reactivate

        bulk_repo = AsyncMock()
        bulk_repo.find_by_ids_and_owner.return_value = [doc]
        bulk_repo.update_by_ids_and_owner.return_value = 1
        bulk = make_bulk_service(bulk_repo)
        await bulk.bulk_set_expiry([_oid(1)], FUTURE, USER_OID)
        _, _, bulk_set = bulk_repo.update_by_ids_and_owner.call_args[0]
        assert bulk_set["status"] == UrlStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_clearing_expiry_reactivates_expired_on_both_paths(self):
        doc = make_url_v2_doc(url_id=_oid(1), status="EXPIRED", expire_after=FUTURE)

        single_repo = AsyncMock()
        single_repo.find_by_id.return_value = doc
        single = make_single_item_service(single_repo, AsyncMock())
        await single.update(_oid(1), UpdateUrlRequest(expire_after=None), USER_OID)
        single_set = single_repo.update.call_args[0][1]["$set"]
        assert single_set["expire_after"] is None
        assert single_set["status"] == UrlStatus.ACTIVE

        bulk_repo = AsyncMock()
        bulk_repo.find_by_ids_and_owner.return_value = [doc]
        bulk_repo.update_by_ids_and_owner.return_value = 1
        bulk = make_bulk_service(bulk_repo)
        await bulk.bulk_set_expiry([_oid(1)], None, USER_OID)
        _, _, bulk_set = bulk_repo.update_by_ids_and_owner.call_args[0]
        assert bulk_set["expire_after"] is None
        assert bulk_set["status"] == UrlStatus.ACTIVE


# ─────────────────────────────────────────────────────────────────────────────
# bulk_move_domain
# ─────────────────────────────────────────────────────────────────────────────

TENANT = "links.acme.com"


class TestBulkMoveDomain:
    @pytest.mark.asyncio
    async def test_mixed_batch_end_to_end(self):
        movable = make_url_v2_doc(url_id=_oid(1), alias="promo")
        on_target = make_url_v2_doc(url_id=_oid(2), alias="there", domain=TENANT)
        taken = make_url_v2_doc(url_id=_oid(3), alias="taken")
        blocked = make_url_v2_doc(url_id=_oid(4), alias="bad", status="BLOCKED")
        url_repo = AsyncMock()
        url_repo.find_by_ids_and_owner.return_value = [
            movable,
            on_target,
            taken,
            blocked,
        ]
        url_repo.update_by_ids_and_owner.return_value = 1
        url_cache = AsyncMock()
        url_service = AsyncMock()
        url_service.check_alias_available.side_effect = lambda alias, domain: (
            alias != "taken"
        )
        svc = make_bulk_service(url_repo, url_cache, url_service=url_service)

        report = await svc.bulk_move_domain(
            [_oid(1), _oid(2), _oid(3), _oid(4), _oid(5)], TENANT, USER_OID
        )

        by_id = {row.id: row for row in report.results}
        assert by_id[str(_oid(1))].ok is True
        assert by_id[str(_oid(2))].ok is True  # already on target: no-op
        assert by_id[str(_oid(3))].error_code == "conflict"
        assert (
            by_id[str(_oid(3))].error == f"Alias 'taken' is already in use on {TENANT}"
        )
        assert by_id[str(_oid(4))].error_code == "forbidden"
        assert by_id[str(_oid(5))].error_code == "not_found"
        # Exactly one write: the movable item, ownership included.
        url_repo.update_by_ids_and_owner.assert_awaited_once()
        ids, owner, set_ops = url_repo.update_by_ids_and_owner.call_args[0]
        assert ids == [_oid(1)] and owner == USER_OID
        assert set_ops["domain"] == TENANT and "updated_at" in set_ops
        # Both keys invalidated for the moved item, grouped per domain.
        invalidated = {
            (tuple(call.args[0]), call.args[1])
            for call in url_cache.invalidate_many.await_args_list
        }
        assert invalidated == {
            (("promo",), SYSTEM_DEFAULT_DOMAIN),
            (("promo",), TENANT),
        }

    @pytest.mark.asyncio
    async def test_no_op_items_write_nothing(self):
        doc = make_url_v2_doc(url_id=_oid(1), domain=TENANT)
        url_repo = AsyncMock()
        url_repo.find_by_ids_and_owner.return_value = [doc]
        svc = make_bulk_service(url_repo)

        report = await svc.bulk_move_domain([_oid(1)], TENANT, USER_OID)

        assert report.results[0].ok is True
        url_repo.update_by_ids_and_owner.assert_not_called()

    @pytest.mark.asyncio
    async def test_null_target_means_system_default(self):
        doc = make_url_v2_doc(url_id=_oid(1), alias="back", domain=TENANT)
        url_repo = AsyncMock()
        url_repo.find_by_ids_and_owner.return_value = [doc]
        url_repo.update_by_ids_and_owner.return_value = 1
        svc = make_bulk_service(url_repo)

        report = await svc.bulk_move_domain([_oid(1)], None, USER_OID)

        assert report.results[0].ok is True
        _, _, set_ops = url_repo.update_by_ids_and_owner.call_args[0]
        assert set_ops["domain"] == SYSTEM_DEFAULT_DOMAIN

    @pytest.mark.asyncio
    async def test_reserved_alias_cannot_move_onto_system_default(self):
        doc = make_url_v2_doc(url_id=_oid(1), alias="login", domain=TENANT)
        url_repo = AsyncMock()
        url_repo.find_by_ids_and_owner.return_value = [doc]
        svc = make_bulk_service(url_repo)

        with patch("services.bulk_url_service.is_reserved_alias", return_value=True):
            report = await svc.bulk_move_domain([_oid(1)], None, USER_OID)

        row = report.results[0]
        assert row.error_code == "validation_error"
        assert row.error == f"Alias 'login' is reserved on {SYSTEM_DEFAULT_DOMAIN}"
        url_repo.update_by_ids_and_owner.assert_not_called()

    @pytest.mark.asyncio
    async def test_intra_batch_same_alias_first_wins(self):
        first = make_url_v2_doc(url_id=_oid(1), alias="promo")
        second = make_url_v2_doc(
            url_id=_oid(2), alias="promo", domain="other.tenant.com"
        )
        url_repo = AsyncMock()
        url_repo.find_by_ids_and_owner.return_value = [first, second]
        url_repo.update_by_ids_and_owner.return_value = 1
        svc = make_bulk_service(url_repo)

        report = await svc.bulk_move_domain([_oid(1), _oid(2)], TENANT, USER_OID)

        by_id = {row.id: row for row in report.results}
        assert by_id[str(_oid(1))].ok is True
        assert by_id[str(_oid(2))].error_code == "conflict"
        # Only the winner was written.
        assert url_repo.update_by_ids_and_owner.await_count == 1

    @pytest.mark.asyncio
    async def test_duplicate_key_race_reports_conflict(self):
        """The unique (domain, alias) index is the authoritative arbiter
        when the availability check races a third-party claim."""
        from pymongo.errors import DuplicateKeyError

        doc = make_url_v2_doc(url_id=_oid(1), alias="promo")
        url_repo = AsyncMock()
        url_repo.find_by_ids_and_owner.return_value = [doc]
        url_repo.update_by_ids_and_owner.side_effect = DuplicateKeyError("dup")
        svc = make_bulk_service(url_repo)

        report = await svc.bulk_move_domain([_oid(1)], TENANT, USER_OID)

        assert report.results[0].error_code == "conflict"

    @pytest.mark.asyncio
    async def test_write_failure_is_internal_and_invalidates_both_keys(self):
        doc = make_url_v2_doc(url_id=_oid(1), alias="promo")
        url_repo = AsyncMock()
        url_repo.find_by_ids_and_owner.return_value = [doc]
        url_repo.update_by_ids_and_owner.side_effect = PyMongoError("boom")
        url_cache = AsyncMock()
        svc = make_bulk_service(url_repo, url_cache)

        report = await svc.bulk_move_domain([_oid(1)], TENANT, USER_OID)

        assert report.results[0].error_code == "internal"
        invalidated = {
            (tuple(call.args[0]), call.args[1])
            for call in url_cache.invalidate_many.await_args_list
        }
        assert invalidated == {
            (("promo",), SYSTEM_DEFAULT_DOMAIN),
            (("promo",), TENANT),
        }

    @pytest.mark.asyncio
    async def test_edge_effects_off_and_onto_system_domain(self):
        """Off-moves purge the old system key (og re-home + plain promoted
        entries); onto-moves re-put og entries under the system key."""
        off_plain = make_url_v2_doc(url_id=_oid(1), alias="hot")
        off_og = make_url_v2_doc(url_id=_oid(2), alias="card", meta_tags={"title": "T"})
        url_repo = AsyncMock()
        url_repo.find_by_ids_and_owner.return_value = [off_plain, off_og]
        url_repo.update_by_ids_and_owner.return_value = 1
        kv = make_kv()
        svc = make_bulk_service(url_repo, kv=kv)

        await svc.bulk_move_domain([_oid(1), _oid(2)], TENANT, USER_OID)
        await drain_edge_tasks(svc)

        # Both old system keys purged; nothing re-put (target is a tenant).
        assert sorted(kv.bulk_delete.call_args[0][0]) == [
            f"cache:{SYSTEM_DEFAULT_DOMAIN}:card",
            f"cache:{SYSTEM_DEFAULT_DOMAIN}:hot",
        ]
        kv.bulk_put.assert_not_awaited()

        # Now the reverse: a tenant og-link moving ONTO the system default
        # begins KV management (entry rendered from the merged doc).
        onto_og = make_url_v2_doc(
            url_id=_oid(3), alias="incoming", domain=TENANT, meta_tags={"title": "T"}
        )
        url_repo2 = AsyncMock()
        url_repo2.find_by_ids_and_owner.return_value = [onto_og]
        url_repo2.update_by_ids_and_owner.return_value = 1
        kv2 = make_kv()
        svc2 = make_bulk_service(url_repo2, kv=kv2)

        with patch(
            "services.bulk_url_service.build_og_entry",
            return_value=(f"cache:{SYSTEM_DEFAULT_DOMAIN}:incoming", "{}"),
        ) as build:
            await svc2.bulk_move_domain([_oid(3)], None, USER_OID)
            await drain_edge_tasks(svc2)

        build.assert_called_once()
        assert build.call_args[0][0].domain == SYSTEM_DEFAULT_DOMAIN
        kv2.bulk_put.assert_awaited_once()
        kv2.bulk_delete.assert_not_awaited()  # old key was a tenant key


class TestMoveDomainParity:
    """UrlService.update(domain-only) vs bulk_move_domain-of-one."""

    @pytest.mark.asyncio
    async def test_move_writes_same_fields_and_invalidates_both_keys(self):
        doc = make_url_v2_doc(url_id=_oid(1), alias="promo")

        single_repo, single_cache = AsyncMock(), AsyncMock()
        single_repo.find_by_id.return_value = doc
        single_repo.check_alias_exists.return_value = False
        single = make_single_item_service(single_repo, single_cache)
        await single.update(
            _oid(1),
            UpdateUrlRequest(domain=TENANT),
            USER_OID,
        )
        single_set = single_repo.update.call_args[0][1]["$set"]

        bulk_repo, bulk_cache = AsyncMock(), AsyncMock()
        bulk_repo.find_by_ids_and_owner.return_value = [doc]
        bulk_repo.update_by_ids_and_owner.return_value = 1
        bulk = make_bulk_service(bulk_repo, bulk_cache)
        await bulk.bulk_move_domain([_oid(1)], TENANT, USER_OID)
        _, _, bulk_set = bulk_repo.update_by_ids_and_owner.call_args[0]

        assert (
            set(single_set.keys()) == set(bulk_set.keys()) == {"domain", "updated_at"}
        )
        assert single_set["domain"] == bulk_set["domain"] == TENANT
        # Single-item clears old key then new key; bulk groups the same
        # two keys per domain.
        single_keys = {call.args for call in single_cache.invalidate.await_args_list}
        assert single_keys == {
            ("promo", SYSTEM_DEFAULT_DOMAIN),
            ("promo", TENANT),
        }
        bulk_keys = {
            (alias, call.args[1])
            for call in bulk_cache.invalidate_many.await_args_list
            for alias in call.args[0]
        }
        assert bulk_keys == single_keys

    @pytest.mark.asyncio
    async def test_conflict_and_reserved_messages_match_single_item(self):
        doc = make_url_v2_doc(url_id=_oid(1), alias="promo")

        single_repo = AsyncMock()
        single_repo.find_by_id.return_value = doc
        single_repo.check_alias_exists.return_value = True  # taken on target
        single = make_single_item_service(single_repo, AsyncMock())
        with pytest.raises(
            ConflictError, match=f"Alias 'promo' is already in use on {TENANT}"
        ):
            await single.update(_oid(1), UpdateUrlRequest(domain=TENANT), USER_OID)

        bulk_repo = AsyncMock()
        bulk_repo.find_by_ids_and_owner.return_value = [doc]
        url_service = AsyncMock()
        url_service.check_alias_available.return_value = False
        bulk = make_bulk_service(bulk_repo, url_service=url_service)
        report = await bulk.bulk_move_domain([_oid(1)], TENANT, USER_OID)
        row = report.results[0]
        assert row.error_code == "conflict"
        assert row.error == f"Alias 'promo' is already in use on {TENANT}"

        # Reserved alias onto the system default: same message both paths.
        tenant_doc = make_url_v2_doc(url_id=_oid(2), alias="login2", domain=TENANT)
        single_repo2 = AsyncMock()
        single_repo2.find_by_id.return_value = tenant_doc
        single2 = make_single_item_service(single_repo2, AsyncMock())
        with (
            patch("services.url_service.is_reserved_alias", return_value=True),
            pytest.raises(
                ValidationError,
                match=f"Alias 'login2' is reserved on {SYSTEM_DEFAULT_DOMAIN}",
            ),
        ):
            await single2.update(_oid(2), UpdateUrlRequest(domain=None), USER_OID)

        bulk_repo2 = AsyncMock()
        bulk_repo2.find_by_ids_and_owner.return_value = [tenant_doc]
        bulk2 = make_bulk_service(bulk_repo2)
        with patch("services.bulk_url_service.is_reserved_alias", return_value=True):
            report2 = await bulk2.bulk_move_domain([_oid(2)], None, USER_OID)
        row2 = report2.results[0]
        assert row2.error_code == "validation_error"
        assert row2.error == f"Alias 'login2' is reserved on {SYSTEM_DEFAULT_DOMAIN}"

    @pytest.mark.asyncio
    async def test_already_on_target_is_noop_on_both_paths(self):
        doc = make_url_v2_doc(url_id=_oid(1), domain=TENANT)

        single_repo = AsyncMock()
        single_repo.find_by_id.return_value = doc
        single = make_single_item_service(single_repo, AsyncMock())
        result = await single.update(_oid(1), UpdateUrlRequest(domain=TENANT), USER_OID)
        assert result is doc
        single_repo.update.assert_not_called()

        bulk_repo = AsyncMock()
        bulk_repo.find_by_ids_and_owner.return_value = [doc]
        bulk = make_bulk_service(bulk_repo)
        report = await bulk.bulk_move_domain([_oid(1)], TENANT, USER_OID)
        assert report.results[0].ok is True
        bulk_repo.update_by_ids_and_owner.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Webhook event emission — bulk must be indistinguishable from a loop
# ─────────────────────────────────────────────────────────────────────────────


class _CapturingSink:
    def __init__(self) -> None:
        self.events = []

    async def emit(self, event) -> None:
        self.events.append(event)


class TestBulkEventEmission:
    @pytest.mark.asyncio
    async def test_set_status_emits_one_updated_per_changed_item(self):
        active = make_url_v2_doc(url_id=_oid(1), alias="one")
        already = make_url_v2_doc(url_id=_oid(2), alias="two", status="INACTIVE")
        url_repo = AsyncMock()
        url_repo.find_by_ids_and_owner.return_value = [active, already]
        url_repo.update_by_ids_and_owner.return_value = 1
        sink = _CapturingSink()
        svc = make_bulk_service(url_repo, AsyncMock(), events=sink)

        await svc.bulk_set_status([_oid(1), _oid(2)], UrlStatus.INACTIVE, USER_OID)
        await drain_edge_tasks(svc)

        # One event for the changed item; the same-status no-op emits nothing.
        assert len(sink.events) == 1
        event = sink.events[0]
        assert event.type == "link.updated"
        assert event.owner_id == str(USER_OID)
        assert event.data["link"]["status"] == "INACTIVE"  # post-state snapshot
        assert event.data["changes"]["status"] == {"old": "ACTIVE", "new": "INACTIVE"}

    @pytest.mark.asyncio
    async def test_delete_emits_one_deleted_per_item(self):
        docs = [
            make_url_v2_doc(url_id=_oid(1), alias="one"),
            make_url_v2_doc(url_id=_oid(2), alias="two"),
        ]
        url_repo = AsyncMock()
        url_repo.find_by_ids_and_owner.return_value = docs
        url_repo.delete_by_ids_and_owner.return_value = 2
        sink = _CapturingSink()
        svc = make_bulk_service(url_repo, AsyncMock(), events=sink)

        await svc.bulk_delete([_oid(1), _oid(2)], USER_OID)
        await drain_edge_tasks(svc)

        assert [e.type for e in sink.events] == ["link.deleted", "link.deleted"]
        assert {e.data["link"]["alias"] for e in sink.events} == {"one", "two"}
