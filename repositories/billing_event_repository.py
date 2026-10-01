"""Repository for ``billing_events``: the webhook dedupe ledger."""

from __future__ import annotations

from datetime import datetime, timezone

from pymongo.errors import DuplicateKeyError

from repositories.base import BaseRepository
from schemas.models.billing_event import BillingEventDoc, BillingEventOutcome


class BillingEventRepository(BaseRepository[BillingEventDoc]):
    async def insert_new(self, event: BillingEventDoc) -> bool:
        """Record a delivery. False means this event id was already recorded."""
        try:
            await self._insert(event.to_mongo())
        except DuplicateKeyError:
            return False
        return True

    async def set_outcome(
        self, event_id: str, outcome: BillingEventOutcome, detail: str | None = None
    ) -> None:
        await self._update(
            {"event_id": event_id},
            {
                "$set": {
                    "outcome": outcome.value,
                    "detail": detail,
                    "processed_at": datetime.now(timezone.utc),
                }
            },
        )

    async def find_by_event_id(self, event_id: str) -> BillingEventDoc | None:
        return await self._find_one({"event_id": event_id})
