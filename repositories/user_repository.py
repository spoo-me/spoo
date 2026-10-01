"""
Repository for the `users` MongoDB collection.

All methods are async and return typed Pydantic document models (UserDoc).
Errors are handled by BaseRepository.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bson import ObjectId
from pymongo.errors import PyMongoError

from infrastructure.logging import get_logger
from repositories.base import BaseRepository
from schemas.models.user import UserDoc, UserStatus

log = get_logger(__name__)


def _purge_due_clauses(now: datetime, lease_seconds: int) -> list[dict]:
    """The two ways an account can be purge-due, as ``$or`` branches:
    PENDING_DELETION past its deadline, or ERASING whose claim lease has
    expired (a crashed cascade). An ERASING doc inside its lease is a LIVE
    cascade — deliberately never matched, so the sweep can't run it twice.
    Shared by ``claim_for_erasure`` and ``find_purge_due`` so the sweep's
    view and the claim's guard can never drift apart.
    """
    return [
        {
            "status": UserStatus.PENDING_DELETION.value,
            "purge_after": {"$lte": now},
        },
        {
            "status": UserStatus.ERASING.value,
            "erasure_claimed_at": {"$lte": now - timedelta(seconds=lease_seconds)},
        },
    ]


class UserRepository(BaseRepository[UserDoc]):
    async def find_by_email(self, email: str) -> UserDoc | None:
        """Find a user by email address."""
        return await self._find_one({"email": email})

    async def find_by_id(self, user_id: ObjectId) -> UserDoc | None:
        """Find a user by ObjectId."""
        return await self._find_one({"_id": user_id})

    async def mark_pro_onboarded(self, user_id: ObjectId) -> bool:
        """Stamp the first Pro tour completion; later calls change nothing."""
        return await self._update_modified(
            {"_id": user_id, "pro_onboarded_at": None},
            {"$set": {"pro_onboarded_at": datetime.now(timezone.utc)}},
        )

    async def find_by_oauth_provider(
        self, provider: str, provider_user_id: str
    ) -> UserDoc | None:
        """Find a user by their OAuth provider and provider-issued user ID.

        Uses ``$elemMatch`` to ensure both fields match the same array
        element — without it, MongoDB can satisfy each condition from
        different elements when a user has multiple linked providers.
        """
        return await self._find_one(
            {
                "auth_providers": {
                    "$elemMatch": {
                        "provider": provider,
                        "provider_user_id": provider_user_id,
                    }
                }
            }
        )

    async def create(self, user_data: dict) -> ObjectId:
        """Insert a new user document. Returns the inserted _id."""
        return await self._insert(user_data)

    async def update(self, user_id: ObjectId, update_ops: dict) -> bool:
        """Apply a MongoDB update document to a user.

        Returns True if the document was matched.
        """
        return await self._update({"_id": user_id}, update_ops)

    async def complete_onboarding(
        self, user_id: ObjectId, when: datetime, heard_from: str | None
    ) -> bool:
        """Stamp ``onboarded_at`` (and HDYHAU) — first completion wins.

        The filter matches only while ``onboarded_at`` is null, so repeat
        calls are no-ops and can never overwrite the original timestamp or
        attribution. Returns True when THIS call did the stamping.
        """
        ops: dict = {"onboarded_at": when}
        if heard_from:
            ops["heard_from"] = heard_from
        return await self._update(
            {"_id": user_id, "onboarded_at": None},
            {"$set": ops},
        )

    async def set_storage_prefix_if_absent(
        self, user_id: ObjectId, prefix: str
    ) -> bool:
        """Pin the R2 owner-key prefix on first upload — one guarded write.

        The prefix derives from SECRET_KEY, so a rotation would orphan
        everything stored under the old value; persisting it on first use
        keeps the erasure sweep pointed at where the objects actually live.
        The absence guard makes concurrent first uploads first-write-wins.
        Returns True when THIS call did the pinning.
        """
        return await self._update_modified(
            {"_id": user_id, "storage_prefix": None},
            {"$set": {"storage_prefix": prefix}},
        )

    async def mark_pending_deletion(
        self, user_id: ObjectId, grace_days: int, *, now: datetime | None = None
    ) -> bool:
        """Flip an ACTIVE account to PENDING_DELETION with a purge deadline.

        Guarded transition — the filter matches ACTIVE only, so a repeat
        request (or one against an INACTIVE account) is a no-op. Returns
        True only when THIS call performed the flip. ``now`` lets the caller
        pin the deadline to a timestamp it already committed elsewhere (the
        OAuth-only restore token's expiry must equal the stored
        ``purge_after`` exactly).
        """
        now = now or datetime.now(timezone.utc)
        return await self._update_modified(
            {"_id": user_id, "status": UserStatus.ACTIVE.value},
            {
                "$set": {
                    "status": UserStatus.PENDING_DELETION.value,
                    "deletion_requested_at": now,
                    "purge_after": now + timedelta(days=grace_days),
                }
            },
        )

    async def claim_for_erasure(
        self, user_id: ObjectId, now: datetime, lease_seconds: int
    ) -> bool:
        """Atomically claim a purge-due account for the erasure cascade.

        Guarded transition: a PENDING_DELETION doc whose ``purge_after``
        has passed matches, so a restore that landed after sweep selection
        wins — the claim fails and the account survives. An ERASING doc is
        re-claimable only once its ``erasure_claimed_at`` lease has expired
        (a crashed cascade retries on the next sweep); a fresh claim marks
        a LIVE cascade and can never be stolen by a second runner.
        ``purge_after`` is preserved (never unset) so that re-claim still
        matches. Returns True when the claim holds.
        """
        return await self._update(
            {
                "_id": user_id,
                "$or": _purge_due_clauses(now, lease_seconds),
            },
            {
                "$set": {
                    "status": UserStatus.ERASING.value,
                    "erasure_claimed_at": now,
                }
            },
        )

    async def restore(self, user_id: ObjectId) -> bool:
        """Cancel a pending deletion — PENDING_DELETION back to ACTIVE.

        Guarded like ``mark_pending_deletion``: only a PENDING_DELETION doc
        matches, so restoring an ACTIVE (or already-purged) account returns
        False. Clears both deletion timestamps. ERASING docs also refuse —
        once the cascade claims the account, erasure is final; matching
        here would "restore" an aborted half-cascade into an empty account.
        """
        return await self._update_modified(
            {"_id": user_id, "status": UserStatus.PENDING_DELETION.value},
            {
                "$set": {"status": UserStatus.ACTIVE.value},
                # erasure_claimed_at only exists on ERASING docs (unreachable
                # here) — cleared as hygiene against manual ops status edits.
                "$unset": {
                    "deletion_requested_at": "",
                    "purge_after": "",
                    "erasure_claimed_at": "",
                },
            },
        )

    async def find_purge_due(
        self, now: datetime, limit: int, lease_seconds: int
    ) -> list[UserDoc]:
        """Return purge-due users — PENDING_DELETION past their deadline
        plus expired-claim ERASING retries (crashed cascades). ERASING docs
        inside their claim lease are LIVE cascades and stay out of the
        sweep's view. Oldest deadline first, capped at *limit*.

        Still served by the ``pending_deletion_sweep`` partial index: its
        partialFilterExpression (status $in [PENDING_DELETION, ERASING])
        subsumes both $or branches' status equalities, and the first branch
        also bounds the indexed ``purge_after`` key; the ERASING branch's
        ``erasure_claimed_at`` filters residually over the handful of
        ERASING entries the partial index holds.
        """
        try:
            cursor = (
                self._col.find({"$or": _purge_due_clauses(now, lease_seconds)})
                .sort("purge_after", 1)
                .limit(limit)
            )
            docs = await cursor.to_list(length=limit)
            return [UserDoc.from_mongo(d) for d in docs]  # type: ignore[misc]
        except PyMongoError as exc:
            log.error(
                "repo_find_purge_due_failed",
                collection=self._collection_name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    async def delete_hard(self, user_id: ObjectId) -> bool:
        """Permanently delete the user document. Returns True if removed.

        The erasure cascade's LAST step — every satellite collection must be
        cleaned first, so a crash anywhere re-queues the user for the next
        sweep instead of orphaning data. Guarded on ERASING: only a doc the
        cascade claimed via ``claim_for_erasure`` may die, so a stray call
        can never hard-delete a live account.
        """
        return await self._delete({"_id": user_id, "status": UserStatus.ERASING.value})
