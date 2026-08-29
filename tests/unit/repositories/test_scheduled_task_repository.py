"""Unit tests for ScheduledTaskRepository — claim/lease semantics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from repositories.scheduled_task_repository import ScheduledTaskRepository
from schemas.models.scheduled_task import TaskRunResult


def _col() -> AsyncMock:
    col = AsyncMock()
    col.name = "scheduled_tasks"
    return col


class TestEnsureTask:
    @pytest.mark.asyncio
    async def test_upserts_with_set_on_insert_only(self):
        col = _col()
        repo = ScheduledTaskRepository(col)
        nxt = datetime(2026, 8, 17, 11, 0, tzinfo=timezone.utc)

        await repo.ensure_task(
            "feed-sync", schedule="0 * * * *", enabled_default=True, next_run_at=nxt
        )

        args, kwargs = col.update_one.await_args
        assert args[0] == {"_id": "feed-sync"}
        assert kwargs["upsert"] is True
        soi = args[1]["$setOnInsert"]
        assert soi["schedule"] == "0 * * * *"
        assert soi["next_run_at"] == nxt
        assert soi["enabled"] is True
        assert soi["claimed_until"] is None
        # Runtime state must never be clobbered on existing docs.
        assert "$set" not in args[1]

    @pytest.mark.asyncio
    async def test_concurrent_upsert_race_is_success(self):
        """Losing the boot-upsert race is success: the doc exists."""
        col = _col()
        col.update_one = AsyncMock(side_effect=DuplicateKeyError("E11000 dup key"))
        repo = ScheduledTaskRepository(col)

        await repo.ensure_task(
            "feed-sync", schedule="0 * * * *", enabled_default=True, next_run_at=None
        )


class TestClaimDue:
    @pytest.mark.asyncio
    async def test_claim_query_shape_and_lease(self):
        col = _col()
        col.find_one_and_update = AsyncMock(return_value=None)
        repo = ScheduledTaskRepository(col)

        before = datetime.now(timezone.utc)
        assert await repo.claim_due(names=("feed-sync",), lease_seconds=600) is None

        args, kwargs = col.find_one_and_update.await_args
        query, update = args
        assert query["enabled"] is True
        assert "$lte" in query["next_run_at"]
        assert {"claimed_until": None} in query["$or"]
        # Scoped to this runner's handlers — never claims a task it can't run.
        assert query["_id"] == {"$in": ["feed-sync"]}
        lease = update["$set"]["claimed_until"]
        assert lease >= before + timedelta(seconds=599)
        assert kwargs["sort"] == [("next_run_at", 1)]
        assert kwargs["return_document"] == ReturnDocument.AFTER

    @pytest.mark.asyncio
    async def test_claim_stamps_fresh_token_each_claim(self):
        col = _col()
        col.find_one_and_update = AsyncMock(return_value=None)
        repo = ScheduledTaskRepository(col)

        await repo.claim_due(names=("feed-sync",), lease_seconds=60)
        await repo.claim_due(names=("feed-sync",), lease_seconds=60)

        tokens = [
            c.args[1]["$set"]["claim_token"]
            for c in col.find_one_and_update.await_args_list
        ]
        assert all(tokens)
        assert tokens[0] != tokens[1]

    @pytest.mark.asyncio
    async def test_claim_returns_doc_model(self):
        col = _col()
        col.find_one_and_update = AsyncMock(
            return_value={
                "_id": "feed-sync",
                "schedule": "0 * * * *",
                "enabled": True,
                "next_run_at": datetime.now(timezone.utc),
                "claim_token": "tok-1",
            }
        )
        repo = ScheduledTaskRepository(col)
        doc = await repo.claim_due(names=("feed-sync",), lease_seconds=60)
        assert doc is not None
        assert doc.id == "feed-sync"
        # The stamped token comes back on the doc to fence finish_run.
        assert doc.claim_token == "tok-1"


class TestFinishRun:
    @pytest.mark.asyncio
    async def test_records_result_and_releases_lease(self):
        col = _col()
        col.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
        repo = ScheduledTaskRepository(col)
        result = TaskRunResult(
            at=datetime.now(timezone.utc), status="ok", duration_ms=42, detail=None
        )
        nxt = datetime.now(timezone.utc) + timedelta(hours=1)

        finished = await repo.finish_run(
            "feed-sync",
            claim_token="tok-1",
            result=result,
            schedule="0 * * * *",
            next_run_at=nxt,
        )

        assert finished is True
        args, _ = col.update_one.await_args
        # Fenced to the executing claim, not just the task name.
        assert args[0] == {"_id": "feed-sync", "claim_token": "tok-1"}
        st = args[1][0]["$set"]
        assert st["last_run"]["$literal"]["status"] == "ok"
        assert st["next_run_at"] == {
            "$cond": [{"$eq": ["$schedule", "0 * * * *"]}, nxt, "$next_run_at"]
        }
        assert st["claimed_until"] is None
        assert st["claim_token"] is None

    @pytest.mark.asyncio
    async def test_reconciled_schedule_keeps_its_next_run_at(self):
        """A mid-run schedule reconcile keeps its own next_run_at ($cond)."""
        col = _col()
        col.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
        repo = ScheduledTaskRepository(col)
        result = TaskRunResult(
            at=datetime.now(timezone.utc), status="ok", duration_ms=42, detail=None
        )
        old_schedule = "0 0 1 1 *"
        stale_next = datetime.now(timezone.utc) + timedelta(days=365)

        finished = await repo.finish_run(
            "feed-sync",
            claim_token="tok-old",
            result=result,
            schedule=old_schedule,
            next_run_at=stale_next,
        )

        assert finished is True
        args, _ = col.update_one.await_args
        assert args[0] == {"_id": "feed-sync", "claim_token": "tok-old"}
        st = args[1][0]["$set"]
        cond, then, otherwise = st["next_run_at"]["$cond"]
        # Reconciled doc: stored schedule differs, stored next_run_at survives.
        assert cond == {"$eq": ["$schedule", old_schedule]}
        assert then == stale_next
        assert otherwise == "$next_run_at"
        # The lease is released unconditionally either way.
        assert st["claimed_until"] is None
        assert st["claim_token"] is None
        assert st["last_run"]["$literal"]["status"] == "ok"

    @pytest.mark.asyncio
    async def test_stale_finisher_noops(self):
        """A finisher whose lease was re-claimed matches nothing and writes nothing."""
        col = _col()
        col.update_one = AsyncMock(return_value=MagicMock(modified_count=0))
        repo = ScheduledTaskRepository(col)
        result = TaskRunResult(
            at=datetime.now(timezone.utc), status="ok", duration_ms=999, detail=None
        )

        finished = await repo.finish_run(
            "feed-sync",
            claim_token="stale",
            result=result,
            schedule="0 * * * *",
            next_run_at=None,
        )

        assert finished is False


class TestInvokeNow:
    @pytest.mark.asyncio
    async def test_arms_next_run_at(self):
        col = _col()
        col.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
        repo = ScheduledTaskRepository(col)
        assert await repo.invoke_now("feed-sync") is True
        args, _ = col.update_one.await_args
        assert args[0] == {"_id": "feed-sync"}
        assert "next_run_at" in args[1]["$set"]

    @pytest.mark.asyncio
    async def test_unknown_task_returns_false(self):
        col = _col()
        col.update_one = AsyncMock(return_value=MagicMock(modified_count=0))
        repo = ScheduledTaskRepository(col)
        assert await repo.invoke_now("nope") is False


class TestReconcileSchedule:
    @pytest.mark.asyncio
    async def test_updates_only_when_schedule_differs(self):
        col = _col()
        col.update_one = AsyncMock(return_value=MagicMock(modified_count=0))
        repo = ScheduledTaskRepository(col)
        changed = await repo.reconcile_schedule(
            "feed-sync", schedule="0 * * * *", next_run_at=None
        )
        assert changed is False
        args, _ = col.update_one.await_args
        # Guarded update: the filter excludes docs already carrying the schedule.
        assert args[0] == {"_id": "feed-sync", "schedule": {"$ne": "0 * * * *"}}
