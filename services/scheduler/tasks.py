"""Built-in task registrations.

``build_task_registry`` is the single composition point for scheduled
tasks — the app (dependencies/wiring.py) and the click worker both build
their registry here so a task registered once exists in whichever process
hosts the runner. Feature tasks take their dependencies as arguments and
gate themselves on their own settings.
"""

from __future__ import annotations

from collections.abc import Sequence

from infrastructure.logging import get_logger
from services.scheduler.registry import ScheduledTask, TaskRegistry

log = get_logger(__name__)

HEARTBEAT_TASK = "scheduler-heartbeat"


async def _heartbeat() -> dict | None:
    """Proof of life for the whole scheduling machine. An hourly
    task_run_completed for this task is the dead-man's-switch signal an
    external monitor can alert on; silence means the runner is down,
    wedged, or misconfigured — regardless of cause."""
    return {}


def build_task_registry(
    extra: Sequence[ScheduledTask] = (),
) -> TaskRegistry:
    """Built-in tasks plus the caller's feature tasks. The caller (wiring
    or the worker runtime) constructs feature tasks with their own
    dependencies and passes them in — this module never imports features."""
    registry = TaskRegistry()
    registry.register(
        ScheduledTask(name=HEARTBEAT_TASK, fn=_heartbeat, schedule="0 * * * *")
    )
    for task in extra:
        registry.register(task)
    return registry
