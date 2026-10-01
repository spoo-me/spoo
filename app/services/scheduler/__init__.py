"""Scheduled-task system: registry + Mongo-lease runner.

The scheduling model is poll = clock = claim: there is no separate beat
process. Every poll interval the runner claims any enabled task whose
``next_run_at`` is due, using the same atomic lease pattern as the webhook
delivery executor, so N concurrent runners (worker + embedded app, during a
deploy overlap) never double-run a task.
"""

from app.services.scheduler.registry import (
    ScheduledTask,
    TaskRegistry,
    compute_next_run,
)
from app.services.scheduler.runner import TaskScheduler

__all__ = ["ScheduledTask", "TaskRegistry", "TaskScheduler", "compute_next_run"]
