"""Unit tests for the scheduler task registry and next-run math."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from services.scheduler.registry import (
    ScheduledTask,
    TaskRegistry,
    compute_next_run,
)


async def _noop() -> dict | None:
    return None


class TestTaskRegistry:
    def test_register_and_get(self):
        reg = TaskRegistry()
        task = ScheduledTask(name="feed-sync", fn=_noop, schedule="0 * * * *")
        reg.register(task)
        assert reg.get("feed-sync") is task
        assert reg.all() == (task,)

    def test_duplicate_name_rejected(self):
        reg = TaskRegistry()
        reg.register(ScheduledTask(name="a", fn=_noop, schedule="0 * * * *"))
        with pytest.raises(ValueError, match="already registered"):
            reg.register(ScheduledTask(name="a", fn=_noop, schedule="0 * * * *"))

    def test_invalid_cron_rejected_at_register(self):
        reg = TaskRegistry()
        with pytest.raises(ValueError, match="cron"):
            reg.register(ScheduledTask(name="bad", fn=_noop, schedule="not a cron"))

    def test_six_field_cron_rejected(self):
        """Six-field cron is a boot error, not a subtly different schedule."""
        reg = TaskRegistry()
        with pytest.raises(ValueError, match="5 cron fields"):
            reg.register(ScheduledTask(name="six", fn=_noop, schedule="*/10 * * * * *"))

    def test_impossible_date_rejected_at_register(self):
        """Feb 31 must be a boot error, whichever way CronSim reports it."""
        reg = TaskRegistry()
        with pytest.raises(ValueError, match="cron"):
            reg.register(ScheduledTask(name="never", fn=_noop, schedule="0 0 31 2 *"))

    def test_manual_only_task_has_no_schedule(self):
        reg = TaskRegistry()
        reg.register(ScheduledTask(name="manual", fn=_noop, schedule=None))
        assert reg.get("manual").schedule is None

    def test_unknown_name_returns_none(self):
        assert TaskRegistry().get("nope") is None


class TestComputeNextRun:
    def test_every_15_minutes(self):
        now = datetime(2026, 8, 17, 10, 7, 30, tzinfo=timezone.utc)
        nxt = compute_next_run("*/15 * * * *", now)
        assert nxt == datetime(2026, 8, 17, 10, 15, tzinfo=timezone.utc)

    def test_hourly(self):
        now = datetime(2026, 8, 17, 10, 0, 0, tzinfo=timezone.utc)
        nxt = compute_next_run("0 * * * *", now)
        assert nxt == datetime(2026, 8, 17, 11, 0, tzinfo=timezone.utc)

    def test_none_schedule_is_manual_only(self):
        assert compute_next_run(None, datetime.now(timezone.utc)) is None

    def test_result_is_utc_aware(self):
        nxt = compute_next_run("*/5 * * * *", datetime.now(timezone.utc))
        assert nxt is not None
        assert nxt.tzinfo is not None
