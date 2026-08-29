"""Document model for the ``scheduled_tasks`` collection.

One doc per registered task; ``_id`` is the task NAME (string), not an
ObjectId — the name is the natural unique key and the doc is upserted by
registry sync at startup. Runtime state (enabled, next_run_at, lease,
last_run) lives here; the task's code and schedule source of truth live in
the in-process registry (services/scheduler/registry.py).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from schemas.models.base import MongoBaseModel


class TaskRunResult(BaseModel):
    """Outcome of the most recent run, embedded on the task doc. Run
    history beyond the last run lives in logs (task_run_completed /
    task_run_failed events)."""

    at: datetime
    status: Literal["ok", "error"]
    duration_ms: int = Field(ge=0)
    detail: str | None = None


class ScheduledTaskDoc(MongoBaseModel):
    id: str | None = Field(default=None, alias="_id")
    # 5-field cron in UTC; None = manual-only (runs only via invoke_now).
    schedule: str | None = None
    next_run_at: datetime | None = None
    # Lease: a claimed task is invisible to other runners until this passes.
    claimed_until: datetime | None = None
    # Fence: stamped by every claim, checked by finish_run — a superseded run
    # can't clobber the active claim.
    claim_token: str | None = None
    # Ops-facing pause switch; initialized from the registration default,
    # never overwritten by registry sync.
    enabled: bool = True
    last_run: TaskRunResult | None = None
    updated_at: datetime | None = None
