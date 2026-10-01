"""Task registry and next-run math.

Code is the source of truth for WHAT tasks exist and WHEN they recur; Mongo
(``scheduled_tasks``) is the source of truth for runtime state (enabled,
next_run_at, lease, last_run). Handlers are plain async callables returning
an optional detail dict — they must be idempotent, because execution is
at-least-once (a crashed run re-claims after its lease expires).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from cronsim import CronSim, CronSimError

# Handler contract: no args (dependencies are closed over at registration),
# returns an optional detail dict stored on last_run and logged.
TaskFn = Callable[[], Awaitable[dict | None]]


@dataclass(frozen=True)
class ScheduledTask:
    """One registered task.

    schedule: 5-field cron expression evaluated in UTC, or None for a
    manual-only task (runs only when something sets next_run_at, e.g. the
    repo's invoke_now).
    """

    name: str
    fn: TaskFn
    schedule: str | None
    enabled_default: bool = True


def compute_next_run(schedule: str | None, now: datetime) -> datetime | None:
    """Next occurrence of *schedule* strictly after *now*, in UTC.

    None schedule -> None (manual-only). Always computed from the caller's
    "now", never from the previous scheduled slot: an overdue task runs once
    and missed windows are never replayed.
    """
    if schedule is None:
        return None
    it = CronSim(schedule, now.astimezone(timezone.utc))
    return next(it)


class TaskRegistry:
    """Name -> ScheduledTask. Duplicate names and bad cron are boot errors,
    never runtime surprises."""

    def __init__(self) -> None:
        self._tasks: dict[str, ScheduledTask] = {}

    def register(self, task: ScheduledTask) -> None:
        if task.name in self._tasks:
            raise ValueError(f"task {task.name!r} already registered")
        if task.schedule is not None:
            # CronSim also accepts six-field seconds-first syntax; reject it
            # instead of scheduling something subtly different.
            if len(task.schedule.split()) != 5:
                raise ValueError(
                    f"task {task.name!r} schedule {task.schedule!r} must have "
                    "exactly 5 cron fields"
                )
            try:
                # CronSim raises StopIteration after a 50-year search for
                # valid-but-never schedules; make that a boot error.
                next(CronSim(task.schedule, datetime.now(timezone.utc)))
            except CronSimError as exc:
                raise ValueError(
                    f"task {task.name!r} has invalid cron {task.schedule!r}: {exc}"
                ) from exc
            except StopIteration:
                raise ValueError(
                    f"task {task.name!r} cron {task.schedule!r} has no "
                    "occurrence within CronSim's search window"
                ) from None
        self._tasks[task.name] = task

    def get(self, name: str) -> ScheduledTask | None:
        return self._tasks.get(name)

    def all(self) -> tuple[ScheduledTask, ...]:
        return tuple(self._tasks.values())
