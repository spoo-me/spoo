"""Claim and terminal-write contracts of the deliveries repository: the
atomic lease can skip saturated endpoints, and terminal writes report whether
they moved the row out of PENDING so the pending slot is released once."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from bson import ObjectId

from app.repositories.webhook_delivery_repository import WebhookDeliveryRepository
from app.schemas.enums.webhook import DeliveryStatus
from app.schemas.models.webhook import DeliveryAttempt

from .conftest import make_collection

_ROW = ObjectId("dddddddddddddddddddddddd")


def _attempt() -> DeliveryAttempt:
    return DeliveryAttempt(attempted_at=datetime.now(timezone.utc), status_code=204)


class TestTerminalWrites:
    @pytest.mark.asyncio
    async def test_finish_from_pending_reports_transition(self):
        col = make_collection()
        col.find_one_and_update.return_value = {"_id": _ROW, "status": "pending"}
        repo = WebhookDeliveryRepository(col)
        assert (
            await repo.record_attempt_and_finish(
                _ROW, _attempt(), DeliveryStatus.SUCCESS
            )
            is True
        )
        query, ops = col.find_one_and_update.await_args[0]
        assert query == {"_id": _ROW}
        assert ops["$set"]["status"] == DeliveryStatus.SUCCESS.value
        assert ops["$inc"] == {"attempt_count": 1}
        assert ops["$push"]["attempts"]["status_code"] == 204

    @pytest.mark.asyncio
    async def test_finish_of_already_finished_row_reports_no_transition(self):
        """A manual retry re-attempts a FAILED row: the attempt is recorded
        but no pending slot was ever held for it."""
        col = make_collection()
        col.find_one_and_update.return_value = {"_id": _ROW, "status": "failed"}
        repo = WebhookDeliveryRepository(col)
        assert (
            await repo.record_attempt_and_finish(
                _ROW, _attempt(), DeliveryStatus.SUCCESS
            )
            is False
        )
        col.find_one_and_update.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_finish_of_missing_row_is_false(self):
        col = make_collection()
        col.find_one_and_update.return_value = None
        assert (
            await WebhookDeliveryRepository(col).mark_failed(_ROW, "event_expired")
            is False
        )

    @pytest.mark.asyncio
    async def test_mark_failed_records_reason_and_transition(self):
        col = make_collection()
        col.find_one_and_update.return_value = {"_id": _ROW, "status": "pending"}
        assert (
            await WebhookDeliveryRepository(col).mark_failed(_ROW, "endpoint_inactive")
            is True
        )
        _, ops = col.find_one_and_update.await_args[0]
        assert ops["$set"]["status"] == DeliveryStatus.FAILED.value
        assert ops["$push"]["attempts"]["error"] == "endpoint_inactive"


class TestCountPending:
    @pytest.mark.asyncio
    async def test_counts_only_rows_that_hold_a_slot(self):
        col = make_collection()
        col.count_documents.return_value = 3
        assert await WebhookDeliveryRepository(col).count_pending(_ROW) == 3
        query = col.count_documents.await_args[0][0]
        assert query["status"] == DeliveryStatus.PENDING.value
        assert query["is_test"] == {"$ne": True}


class TestClaimDue:
    @pytest.mark.asyncio
    async def test_plain_claim_has_no_endpoint_filter(self):
        col = make_collection()
        col.find_one_and_update.return_value = None
        assert await WebhookDeliveryRepository(col).claim_due() is None
        query = col.find_one_and_update.await_args[0][0]
        assert query["status"] == DeliveryStatus.PENDING.value
        assert "endpoint_id" not in query

    @pytest.mark.asyncio
    async def test_exclusions_become_nin(self):
        col = make_collection()
        col.find_one_and_update.return_value = None
        saturated = [ObjectId(), ObjectId()]
        await WebhookDeliveryRepository(col).claim_due(exclude_endpoints=saturated)
        query = col.find_one_and_update.await_args[0][0]
        assert query["endpoint_id"] == {"$nin": saturated}
