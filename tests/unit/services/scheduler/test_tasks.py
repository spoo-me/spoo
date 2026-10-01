"""Unit tests for build_task_registry composition."""

from __future__ import annotations

from app.services.scheduler.registry import ScheduledTask
from app.services.scheduler.tasks import HEARTBEAT_TASK, build_task_registry


async def _noop() -> dict | None:
    return None


class TestBuildTaskRegistry:
    def test_heartbeat_always_registered(self):
        task = build_task_registry().get(HEARTBEAT_TASK)
        assert task is not None
        assert task.schedule == "0 * * * *"

    def test_no_extra_means_builtins_only(self):
        assert [t.name for t in build_task_registry().all()] == [HEARTBEAT_TASK]

    def test_extra_tasks_registered_alongside_builtins(self):
        extra = ScheduledTask(name="feature-x", fn=_noop, schedule="*/5 * * * *")
        reg = build_task_registry([extra])
        assert reg.get("feature-x") is extra
        assert reg.get(HEARTBEAT_TASK) is not None
