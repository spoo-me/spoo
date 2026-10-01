"""
Repositories for the `reports` and `report_submissions` MongoDB collections.

`reports` holds ONE document per reported (domain, code) — dedupe +
velocity storage, not append-only. Re-reports $inc the counter and
refresh ``last_reported_at`` instead of inserting rows: report velocity
per code is the triage signal the abuse funnel wants. ``status`` starts
at "open"; the resolution pipeline owns transitions and never this layer.

`report_submissions` is the per-POST audit trail — one insert per
submission, keeping the per-code docs clean while preserving who sent
what, when, from where.

Neither collection is read back here yet (the resolution pipeline is out
of scope), so both repositories are write-only and model-less.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from bson import ObjectId
from pymongo.errors import PyMongoError

from app.infrastructure.logging import get_logger
from app.repositories.base import BaseRepository

log = get_logger(__name__)


class ReportRepository(BaseRepository[None]):
    """Write-side access to the per-code `reports` documents."""

    async def record_report(
        self,
        domain: str | None,
        code: str,
        *,
        reason: str,
        vector: str | None,
        details: str | None,
        reporter_id: ObjectId | None,
        source: str,
        now: datetime,
    ) -> None:
        """Upsert one report of (domain, code) — $inc/$addToSet, never insert-per-report.

        ``domain`` is ``None`` for the system default domain, the
        lowercased fqdn for custom-domain links. The (domain, code) pair
        is unique-indexed, so concurrent first reports collapse into one
        document.
        """
        authed_bucket = "authenticated" if reporter_id is not None else "anonymous"
        add_to_set: dict[str, Any] = {"reasons": reason}
        if vector is not None:
            add_to_set["vectors"] = vector
        if reporter_id is not None:
            add_to_set["reporter_ids"] = reporter_id

        set_fields: dict[str, Any] = {"last_reported_at": now}
        if details:
            set_fields["last_details"] = details

        ops = {
            "$inc": {
                "count": 1,
                f"reporters.{authed_bucket}": 1,
                f"source_counts.{source}": 1,
            },
            "$set": set_fields,
            # status starts "open" and is NEVER touched on re-report —
            # the resolution funnel owns transitions.
            "$setOnInsert": {
                "domain": domain,
                "code": code,
                "first_reported_at": now,
                "status": "open",
            },
            "$addToSet": add_to_set,
        }

        try:
            await self._col.update_one(
                {"domain": domain, "code": code}, ops, upsert=True
            )
        except PyMongoError as exc:
            log.error(
                "repo_report_upsert_failed",
                collection=self._collection_name,
                domain=domain,
                code=code[:50],
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    async def pull_reporter(self, reporter_id: ObjectId) -> int:
        """``$pull`` the reporter's id out of every report's ``reporter_ids``.

        Account erasure: the aggregate counters stay (they're anonymous) —
        only the id linking the user to their reports goes. Returns the
        number of documents modified.
        """
        try:
            result = await self._col.update_many(
                {"reporter_ids": reporter_id},
                {"$pull": {"reporter_ids": reporter_id}},
            )
            return result.modified_count
        except PyMongoError as exc:
            log.error(
                "repo_pull_reporter_failed",
                collection=self._collection_name,
                reporter_id=str(reporter_id),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise


class ReportSubmissionRepository(BaseRepository[None]):
    """Append-only audit trail — one document per POST /api/v1/reports."""

    async def insert(self, doc: dict[str, Any]) -> ObjectId:
        """Insert a submission record. Returns the inserted ``_id``
        (the ``submission_id`` echoed on the wire)."""
        return await self._insert(doc)

    async def delete_by_reporter(
        self, reporter_id: ObjectId, reporter_email: str
    ) -> int:
        """Delete every submission traceable to the reporter (account erasure).

        Matches by id OR by the optional follow-up email, so anonymous
        submissions that left a contact address go too. Returns the number
        of documents deleted.
        """
        return await self._delete_many(
            {
                "$or": [
                    {"reporter_id": reporter_id},
                    {"reporter_email": reporter_email},
                ]
            }
        )
