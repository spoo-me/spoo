"""Repository for the ``scheduled_tasks`` collection.

Owns the runner's claim semantics: ``claim_due`` is the same atomic
``find_one_and_update`` lease as the webhook delivery executor, which is
what makes concurrent runners (worker + embedded app during a deploy
overlap) safe with no scheduler infrastructure beyond Mongo itself. The
claim query is stateless over ``next_run_at``, so restarts self-heal and an
overdue task runs exactly once on the first loop after boot.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from infrastructure.logging import get_logger
from repositories.base import BaseRepository
from schemas.models.scheduled_task import ScheduledTaskDoc, TaskRunResult

log = get_logger(__name__)


class ScheduledTaskRepository(BaseRepository[ScheduledTaskDoc]):
    async def ensure_task(
        self,
        name: str,
        *,
        schedule: str | None,
        enabled_default: bool,
        next_run_at: datetime | None,
    ) -> None:
        """Upsert the task doc. ``$setOnInsert`` only — runtime state
        (enabled, next_run_at, last_run) is never clobbered on an existing
        doc; schedule drift is handled by ``reconcile_schedule``."""
        try:
            await self._col.update_one(
                {"_id": name},
                {
                    "$setOnInsert": {
                        "schedule": schedule,
                        "next_run_at": next_run_at,
                        "claimed_until": None,
                        "claim_token": None,
                        "enabled": enabled_default,
                        "last_run": None,
                        "updated_at": datetime.now(timezone.utc),
                    }
                },
                upsert=True,
            )
        except DuplicateKeyError:
            # Lost the boot-upsert race; the doc exists — that's success.
            log.debug("scheduled_task_upsert_race", task=name)

    async def reconcile_schedule(
        self, name: str, *, schedule: str | None, next_run_at: datetime | None
    ) -> bool:
        """Apply a code-side schedule change to the stored doc. Returns True
        when the stored schedule differed and was updated."""
        result = await self._col.update_one(
            {"_id": name, "schedule": {"$ne": schedule}},
            {
                "$set": {
                    "schedule": schedule,
                    "next_run_at": next_run_at,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        return bool(result.modified_count)

    async def claim_due(
        self, *, names: Iterable[str], lease_seconds: int
    ) -> ScheduledTaskDoc | None:
        """Atomically claim one due task among *names*, or None when nothing
        is due; a runner never claims a task it has no handler for.

        BSON type bracketing means a null ``next_run_at`` (manual-only,
        not invoked) never matches the ``$lte`` date comparison. A crashed
        runner's claim expires with its lease — tasks are never stranded.

        Every claim stamps a fresh ``claim_token``; ``finish_run`` is fenced
        on it so a superseded run can't clobber the active claim.
        """
        now = datetime.now(timezone.utc)
        doc = await self._col.find_one_and_update(
            {
                "_id": {"$in": list(names)},
                "enabled": True,
                "next_run_at": {"$lte": now},
                "$or": [
                    {"claimed_until": None},
                    {"claimed_until": {"$lte": now}},
                ],
            },
            {
                "$set": {
                    "claimed_until": now + timedelta(seconds=lease_seconds),
                    "claim_token": uuid4().hex,
                }
            },
            sort=[("next_run_at", 1)],
            return_document=ReturnDocument.AFTER,
        )
        return ScheduledTaskDoc.from_mongo(doc)

    async def finish_run(
        self,
        name: str,
        *,
        claim_token: str | None,
        result: TaskRunResult,
        schedule: str | None,
        next_run_at: datetime | None,
    ) -> bool:
        """Record the outcome, release the lease, arm the next occurrence.
        Fenced on *claim_token*: a superseded finisher is a no-op returning
        False. The $cond applies *next_run_at* only while the stored schedule
        still matches, so a mid-run reconcile keeps its own next_run_at."""
        write = await self._col.update_one(
            {"_id": name, "claim_token": claim_token},
            [
                {
                    "$set": {
                        "last_run": {"$literal": result.model_dump()},
                        "next_run_at": {
                            "$cond": [
                                {"$eq": ["$schedule", schedule]},
                                next_run_at,
                                "$next_run_at",
                            ]
                        },
                        "claimed_until": None,
                        "claim_token": None,
                        "updated_at": datetime.now(timezone.utc),
                    }
                }
            ],
        )
        if not write.modified_count:
            log.info("task_finish_superseded", task=name)
            return False
        return True

    async def invoke_now(self, name: str) -> bool:
        """Run-now invocator. Any caller (ops tooling, tests, future HTTP
        endpoint) schedules an immediate run by arming next_run_at; the
        claim lease still dedupes against the cron path."""
        result = await self._col.update_one(
            {"_id": name},
            {"$set": {"next_run_at": datetime.now(timezone.utc)}},
        )
        return bool(result.modified_count)

    async def list_names(self) -> set[str]:
        docs = await self._col.find({}, {"_id": 1}).to_list(length=None)
        return {d["_id"] for d in docs}
