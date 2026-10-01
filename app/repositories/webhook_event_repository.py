"""Repository for the ``webhook-events`` collection — the fact, stored once.

One row per occurrence regardless of fan-out. TTL-bounded via
the ``created_at`` index; deliveries reference rows here by ``_id``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from bson import ObjectId

from app.repositories.base import BaseRepository
from app.schemas.models.webhook import WebhookEventDoc
from app.services.events.contract import DomainEvent


class WebhookEventRepository(BaseRepository[WebhookEventDoc]):
    async def insert_event(
        self, event: DomainEvent, *, is_test: bool = False
    ) -> ObjectId:
        return await self._insert(
            {
                "event_id": event.event_id,
                "type": event.type,
                "v": event.v,
                "owner_id": ObjectId(event.owner_id),
                "occurred_at": event.occurred_at,
                "payload": event.data,
                "is_test": is_test,
                "created_at": datetime.now(timezone.utc),
            }
        )

    async def find_by_oid(self, oid: ObjectId) -> WebhookEventDoc | None:
        return await self._find_one({"_id": oid})

    async def delete_by_owner(self, owner_id: ObjectId) -> int:
        """Delete every stored event fact for the owner (account erasure).

        Returns the number of documents deleted.
        """
        return await self._delete_many({"owner_id": owner_id})
