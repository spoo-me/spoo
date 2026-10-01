"""
Repository for the ``subscriptions`` collection.

A status change is one operation here: the compare-and-set write, the audit
event, and the owner's cache invalidation happen inside ``write_transition``
so no caller can change a status without the other two.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from errors import ConflictError
from infrastructure.cache.entitlement_cache import EntitlementCache
from repositories.base import BaseRepository
from repositories.entitlement_event_repository import EntitlementEventRepository
from schemas.models.entitlement_event import EntitlementEventDoc, EntitlementEventKind
from schemas.models.subscription import SubscriptionDoc, SubscriptionStatus
from shared.datetime_utils import as_aware_utc

_SNAPSHOT_FIELDS = (
    "status",
    "kind",
    "provider",
    "current_period_end",
    "prepaid_until",
    "grace_until",
    "founding",
    "founding_streak_ok",
)

_EVENT_KIND_FOR_STATUS = {
    SubscriptionStatus.GRACE: EntitlementEventKind.GRACE_STARTED,
    SubscriptionStatus.LAPSED: EntitlementEventKind.LAPSED,
}


def _snapshot(doc: SubscriptionDoc | None) -> dict[str, Any] | None:
    if doc is None:
        return None
    out: dict[str, Any] = {}
    for f in _SNAPSHOT_FIELDS:
        v = getattr(doc, f)
        if isinstance(v, datetime):
            v = as_aware_utc(v)
        out[f] = v.value if hasattr(v, "value") else v
    return out


def _normalise(value: Any) -> Any:
    if isinstance(value, datetime):
        return as_aware_utc(value)
    return value.value if hasattr(value, "value") else value


class SubscriptionRepository(BaseRepository[SubscriptionDoc]):
    def __init__(
        self,
        collection,
        events: EntitlementEventRepository,
        cache: EntitlementCache,
    ) -> None:
        super().__init__(collection)
        self._events = events
        self._cache = cache

    async def find_by_user(self, user_id: ObjectId) -> SubscriptionDoc | None:
        return await self._find_one({"user_id": user_id})

    async def write_transition(
        self,
        user_id: ObjectId,
        *,
        before: SubscriptionDoc | None,
        after_status: SubscriptionStatus,
        fields: dict[str, Any],
        actor: str,
        reason: str,
        now: datetime | None = None,
    ) -> SubscriptionDoc:
        """Persist ``after_status`` plus ``fields`` for the user's subscription.

        ``before`` is the document the caller decided the transition from; the
        write is conditional on the stored status still matching it, so two
        concurrent transitions cannot both win. A write that changes nothing
        returns the current document and records no event.
        """
        now = now or datetime.now(timezone.utc)
        changes = {k: v for k, v in fields.items() if k not in ("user_id", "status")}
        if before is not None:
            unchanged = before.status == after_status and all(
                _normalise(getattr(before, k, None)) == _normalise(v)
                for k, v in changes.items()
            )
            if unchanged:
                return before

        if before is None:
            doc = SubscriptionDoc(
                user_id=user_id,
                status=after_status,
                created_at=now,
                updated_at=now,
                **changes,
            )
            try:
                inserted = await self._insert(doc.to_mongo())
            except DuplicateKeyError:
                raise ConflictError("subscription changed concurrently") from None
            after = doc.model_copy(update={"id": inserted})
        else:
            matched = await self._update(
                {"user_id": user_id, "status": before.status.value},
                {"$set": {**changes, "status": after_status.value, "updated_at": now}},
            )
            if not matched:
                raise ConflictError("subscription changed concurrently")
            after = SubscriptionDoc.model_validate(
                {
                    **before.model_dump(),
                    **changes,
                    "status": after_status,
                    "updated_at": now,
                }
            )

        kind = EntitlementEventKind.SUBSCRIPTION_CHANGED
        if before is None or before.status != after_status:
            kind = _EVENT_KIND_FOR_STATUS.get(after_status, kind)
        await self._events.append(
            EntitlementEventDoc(
                user_id=user_id,
                kind=kind,
                actor=actor,
                reason=reason,
                before=_snapshot(before),
                after=_snapshot(after),
                at=now,
            )
        )
        await self._cache.invalidate(user_id)
        return after

    async def find_term_ended(self, now: datetime) -> list[SubscriptionDoc]:
        """Paid terms that have run out: a scheduled cancel past its period
        end, or a prepaid year past its end."""
        return await self._find_many(
            {
                "$or": [
                    {
                        "status": SubscriptionStatus.CANCEL_AT_PERIOD_END.value,
                        "current_period_end": {"$lte": now},
                    },
                    {
                        "status": SubscriptionStatus.ACTIVE.value,
                        "kind": "prepaid",
                        "prepaid_until": {"$lte": now},
                    },
                ]
            }
        )

    async def find_grace_ended(self, now: datetime) -> list[SubscriptionDoc]:
        return await self._find_many(
            {"status": SubscriptionStatus.GRACE.value, "grace_until": {"$lte": now}}
        )

    async def find_in_grace(self) -> list[SubscriptionDoc]:
        return await self._find_many({"status": SubscriptionStatus.GRACE.value})

    async def find_lapsed_since(
        self, since: datetime, *, not_by: str
    ) -> list[SubscriptionDoc]:
        """Lapsed recently enough to still be owed the lapsed email, except
        those lapsed by ``not_by``; a lapsed document's last write is the lapse."""
        return await self._find_many(
            {
                "status": SubscriptionStatus.LAPSED.value,
                "updated_at": {"$gte": since},
                "lapsed_by": {"$ne": not_by},
            }
        )

    async def find_past_due_older_than(self, cutoff: datetime) -> list[SubscriptionDoc]:
        return await self._find_many(
            {
                "status": SubscriptionStatus.PAST_DUE.value,
                "current_period_end": {"$lte": cutoff},
            }
        )

    async def find_terms_ending_before(self, until: datetime) -> list[SubscriptionDoc]:
        """Subscriptions whose paid term ends before ``until`` and will not
        renew: prepaid years and scheduled cancels."""
        return await self._find_many(
            {
                "$or": [
                    {
                        "status": SubscriptionStatus.CANCEL_AT_PERIOD_END.value,
                        "current_period_end": {"$lte": until},
                    },
                    {
                        "status": SubscriptionStatus.ACTIVE.value,
                        "kind": "prepaid",
                        "prepaid_until": {"$lte": until},
                    },
                ]
            }
        )

    async def find_provider_live(self, provider: str) -> list[SubscriptionDoc]:
        """Every non-lapsed recurring subscription of a provider, for reconcile."""
        return await self._find_many(
            {
                "provider": provider,
                "kind": "recurring",
                "status": {"$ne": SubscriptionStatus.LAPSED.value},
            },
            limit=5000,
        )

    async def find_by_provider_subscription(
        self, subscription_id: str
    ) -> SubscriptionDoc | None:
        return await self._find_one({"provider_ids.subscription_id": subscription_id})

    async def find_cancel_pending(self) -> list[SubscriptionDoc]:
        return await self._find_many({"provider_ids.cancel_pending": {"$ne": None}})

    async def clear_cancel_pending(self, user_id: ObjectId) -> bool:
        return await self._update(
            {"user_id": user_id}, {"$unset": {"provider_ids.cancel_pending": ""}}
        )

    async def count_founding(self) -> int:
        return await self._count({"founding": True})

    async def delete_by_user(self, user_id: ObjectId) -> int:
        deleted = await self._delete_many({"user_id": user_id})
        if deleted:
            await self._cache.invalidate(user_id)
        return deleted
