"""Unit tests for TaskScheduler — sync, execute, failure isolation."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from pymongo.errors import PyMongoError

from schemas.models.scheduled_task import ScheduledTaskDoc
from services.scheduler.registry import ScheduledTask, TaskRegistry
from services.scheduler.runner import TaskScheduler


def _repo() -> AsyncMock:
    repo = AsyncMock()
    repo.list_names = AsyncMock(return_value=set())
    repo.reconcile_schedule = AsyncMock(return_value=False)
    return repo


def _row(
    name: str,
    schedule: str | None = "0 * * * *",
    claim_token: str | None = None,
) -> ScheduledTaskDoc:
    return ScheduledTaskDoc(
        _id=name,
        schedule=schedule,
        enabled=True,
        next_run_at=datetime.now(timezone.utc),
        claim_token=claim_token,
    )


def _registry_with(name: str, schedule: str | None = "0 * * * *") -> TaskRegistry:
    reg = TaskRegistry()

    async def fn() -> dict | None:
        return None

    reg.register(ScheduledTask(name=name, fn=fn, schedule=schedule))
    return reg


async def _run_until_first_claim(sched: TaskScheduler, repo: AsyncMock) -> None:
    """Drive run() until claim_due has been polled once, then cancel."""
    task = asyncio.create_task(sched.run())
    try:

        async def _wait() -> None:
            while not repo.claim_due.await_count:
                await asyncio.sleep(0.005)

        await asyncio.wait_for(_wait(), timeout=2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestSyncRegistry:
    @pytest.mark.asyncio
    async def test_upserts_every_registration(self):
        reg = TaskRegistry()

        async def fn() -> dict | None:
            return None

        reg.register(ScheduledTask(name="a", fn=fn, schedule="0 * * * *"))
        reg.register(ScheduledTask(name="b", fn=fn, schedule=None))
        repo = _repo()

        await TaskScheduler(repo, reg).sync_registry()

        names = [c.args[0] for c in repo.ensure_task.await_args_list]
        assert names == ["a", "b"]
        # Manual-only task is armed with next_run_at=None.
        b_kwargs = repo.ensure_task.await_args_list[1].kwargs
        assert b_kwargs["next_run_at"] is None

    @pytest.mark.asyncio
    async def test_orphan_docs_logged_not_deleted(self):
        reg = TaskRegistry()
        repo = _repo()
        repo.list_names = AsyncMock(return_value={"ghost"})

        await TaskScheduler(repo, reg).sync_registry()

        repo.delete_by_id.assert_not_awaited()


class TestRun:
    @pytest.mark.asyncio
    async def test_boot_sync_retries_until_success(self):
        """A transient boot-sync error must not kill the scheduler; retry."""
        repo = _repo()
        repo.ensure_task = AsyncMock(side_effect=[PyMongoError("boot flake"), None])
        repo.claim_due = AsyncMock(return_value=None)
        sched = TaskScheduler(repo, _registry_with("a"), poll_interval=0.005)

        await _run_until_first_claim(sched, repo)

        assert repo.ensure_task.await_count == 2

    @pytest.mark.asyncio
    async def test_claim_scoped_to_registered_names(self):
        """A runner only ever claims tasks it has a handler for."""
        repo = _repo()
        repo.claim_due = AsyncMock(return_value=None)
        sched = TaskScheduler(repo, _registry_with("feed-sync"), poll_interval=0.005)

        await _run_until_first_claim(sched, repo)

        assert repo.claim_due.await_args.kwargs["names"] == ("feed-sync",)


class TestExecute:
    @pytest.mark.asyncio
    async def test_success_records_ok_and_next_run(self):
        reg = TaskRegistry()
        calls = []

        async def fn() -> dict | None:
            calls.append(1)
            return {"synced": 3}

        reg.register(ScheduledTask(name="feed-sync", fn=fn, schedule="0 * * * *"))
        repo = _repo()
        sched = TaskScheduler(repo, reg)

        await sched._execute(_row("feed-sync"))

        assert calls == [1]
        kwargs = repo.finish_run.await_args.kwargs
        assert kwargs["result"].status == "ok"
        assert "synced" in (kwargs["result"].detail or "")
        # The executed schedule rides along so finish_run can fence the rearm.
        assert kwargs["schedule"] == "0 * * * *"
        assert kwargs["next_run_at"] is not None
        assert kwargs["next_run_at"] > datetime.now(timezone.utc)

    @pytest.mark.asyncio
    async def test_handler_exception_recorded_not_raised(self):
        reg = TaskRegistry()

        async def fn() -> dict | None:
            raise RuntimeError("feed 500")

        reg.register(ScheduledTask(name="feed-sync", fn=fn, schedule="0 * * * *"))
        repo = _repo()

        await TaskScheduler(repo, reg)._execute(_row("feed-sync"))

        kwargs = repo.finish_run.await_args.kwargs
        assert kwargs["result"].status == "error"
        assert "RuntimeError" in kwargs["result"].detail
        # A failed run still re-arms the next occurrence.
        assert kwargs["next_run_at"] is not None

    @pytest.mark.asyncio
    async def test_manual_only_task_rearms_to_none(self):
        reg = TaskRegistry()

        async def fn() -> dict | None:
            return None

        reg.register(ScheduledTask(name="manual", fn=fn, schedule=None))
        repo = _repo()

        await TaskScheduler(repo, reg)._execute(_row("manual", schedule=None))

        assert repo.finish_run.await_args.kwargs["next_run_at"] is None

    @pytest.mark.asyncio
    async def test_unknown_task_logged_not_finished(self):
        """An unknown task is logged, never finished: the lease expiry self-heals."""
        repo = _repo()
        await TaskScheduler(repo, TaskRegistry())._execute(_row("ghost"))

        repo.finish_run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_finish_is_fenced_to_claim_token(self):
        """The claim token is threaded from claim_due through to finish_run."""
        reg = _registry_with("feed-sync")
        repo = _repo()

        await TaskScheduler(repo, reg)._execute(_row("feed-sync", claim_token="tok-1"))

        assert repo.finish_run.await_args.kwargs["claim_token"] == "tok-1"
