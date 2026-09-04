"""
Repository for ``entitlement_overrides``: one document per (user, key).

Grant and revoke each write the audit event and drop the owner's cached
entitlements in the same operation as the document change.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from bson import ObjectId

from infrastructure.cache.entitlement_cache import EntitlementCache
from repositories.base import BaseRepository
from repositories.entitlement_event_repository import EntitlementEventRepository
from schemas.models.entitlement_event import EntitlementEventDoc, EntitlementEventKind
from schemas.models.entitlement_override import EntitlementOverrideDoc, OverrideKind
from shared.datetime_utils import as_aware_utc

OverrideCheck = Callable[[str, bool | int], None]


def _snapshot(doc: EntitlementOverrideDoc) -> dict:
    return {
        "key": doc.key,
        "value": doc.value,
        "kind": doc.kind.value,
        "expires_at": as_aware_utc(doc.expires_at),
    }


class EntitlementOverrideRepository(BaseRepository[EntitlementOverrideDoc]):
    def __init__(
        self,
        collection,
        events: EntitlementEventRepository,
        cache: EntitlementCache,
        *,
        check: OverrideCheck | None = None,
    ) -> None:
        super().__init__(collection)
        self._events = events
        self._cache = cache
        # The catalog's validator, injected so this layer never imports services.
        self._check = check

    async def list_active(
        self, user_id: ObjectId, now: datetime
    ) -> list[EntitlementOverrideDoc]:
        cursor = self._col.find(
            {
                "user_id": user_id,
                "$or": [{"expires_at": None}, {"expires_at": {"$gt": now}}],
            }
        )
        docs = await cursor.to_list(length=None)
        return [EntitlementOverrideDoc.from_mongo(d) for d in docs]  # type: ignore[misc]

    async def list_for(self, user_id: ObjectId) -> list[EntitlementOverrideDoc]:
        docs = await self._col.find({"user_id": user_id}).to_list(length=None)
        return [EntitlementOverrideDoc.from_mongo(d) for d in docs]  # type: ignore[misc]

    async def grant(
        self,
        user_id: ObjectId,
        key: str,
        value: bool | int,
        *,
        kind: OverrideKind,
        reason: str,
        granted_by: str,
        expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> EntitlementOverrideDoc:
        if self._check is not None:
            self._check(key, value)
        now = now or datetime.now(timezone.utc)
        existing = await self._find_one({"user_id": user_id, "key": key})
        if (
            existing is not None
            and existing.value == value
            and existing.kind is kind
            and as_aware_utc(existing.expires_at) == as_aware_utc(expires_at)
        ):
            return existing

        fields = {
            "value": value,
            "kind": kind.value,
            "reason": reason,
            "granted_by": granted_by,
            "expires_at": expires_at,
            "updated_at": now,
        }
        await self._col.update_one(
            {"user_id": user_id, "key": key},
            {"$set": fields, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
        doc = EntitlementOverrideDoc(
            user_id=user_id,
            key=key,
            value=value,
            kind=kind,
            reason=reason,
            granted_by=granted_by,
            expires_at=expires_at,
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        await self._events.append(
            EntitlementEventDoc(
                user_id=user_id,
                kind=EntitlementEventKind.OVERRIDE_GRANTED,
                actor=granted_by,
                reason=reason,
                before=_snapshot(existing) if existing else None,
                after=_snapshot(doc),
                at=now,
            )
        )
        await self._cache.invalidate(user_id)
        return doc

    async def revoke(
        self,
        user_id: ObjectId,
        key: str,
        *,
        actor: str,
        reason: str,
        now: datetime | None = None,
    ) -> bool:
        now = now or datetime.now(timezone.utc)
        existing = await self._find_one({"user_id": user_id, "key": key})
        if existing is None:
            return False
        if not await self._delete({"_id": existing.id}):
            return False
        await self._events.append(
            EntitlementEventDoc(
                user_id=user_id,
                kind=EntitlementEventKind.OVERRIDE_REVOKED,
                actor=actor,
                reason=reason,
                before=_snapshot(existing),
                after=None,
                at=now,
            )
        )
        await self._cache.invalidate(user_id)
        return True

    async def delete_by_user(self, user_id: ObjectId) -> int:
        deleted = await self._delete_many({"user_id": user_id})
        if deleted:
            await self._cache.invalidate(user_id)
        return deleted
