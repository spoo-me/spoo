"""Repository for the append-only ``entitlement_events`` audit collection."""

from __future__ import annotations

from bson import ObjectId

from repositories.base import BaseRepository
from schemas.models.entitlement_event import EntitlementEventDoc, EntitlementEventKind

_VERSIONLESS_KINDS = (EntitlementEventKind.REMINDER_SENT.value,)


class EntitlementEventRepository(BaseRepository[EntitlementEventDoc]):
    async def append(self, event: EntitlementEventDoc) -> ObjectId:
        return await self._insert(event.to_mongo())

    async def count_for(self, user_id: ObjectId) -> int:
        """The user's entitlement version: one per write that changed what
        they hold. Reminder events are audit only and do not bump it."""
        return await self._count(
            {"user_id": user_id, "kind": {"$nin": list(_VERSIONLESS_KINDS)}}
        )

    async def delete_by_user(self, user_id: ObjectId) -> int:
        return await self._delete_many({"user_id": user_id})
