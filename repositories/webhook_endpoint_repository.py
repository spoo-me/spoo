"""Repository for the ``webhook-endpoints`` collection.

Health counters are updated atomically here; the semantics that matter:
``consecutive_failures`` counts EXHAUSTED deliveries (a delivery that ran
out of retries), never individual attempts — one flaky night must not
kill an endpoint. Success resets it and drains ``dropped_count`` —
the drained value is carried on the next delivery's envelope.
"""

from __future__ import annotations

from datetime import datetime, timezone

from bson import ObjectId

from repositories.base import BaseRepository
from schemas.enums.webhook import EndpointDisabledReason, WebhookStatus
from schemas.models.webhook import WebhookEndpointDoc


class WebhookEndpointRepository(BaseRepository[WebhookEndpointDoc]):
    async def insert_endpoint(self, doc: WebhookEndpointDoc) -> ObjectId:
        return await self._insert(doc.to_mongo())

    async def find_by_id(self, endpoint_id: ObjectId) -> WebhookEndpointDoc | None:
        return await self._find_one({"_id": endpoint_id})

    async def find_owned(
        self, endpoint_id: ObjectId, user_id: ObjectId
    ) -> WebhookEndpointDoc | None:
        return await self._find_one({"_id": endpoint_id, "user_id": user_id})

    async def find_by_user(self, user_id: ObjectId) -> list[WebhookEndpointDoc]:
        docs = await self._col.find({"user_id": user_id}).to_list(length=None)
        return [WebhookEndpointDoc.from_mongo(d) for d in docs]

    async def find_active_for_owner(
        self, owner_id: ObjectId
    ) -> list[WebhookEndpointDoc]:
        docs = await self._col.find(
            {"user_id": owner_id, "status": WebhookStatus.ACTIVE.value}
        ).to_list(length=None)
        return [WebhookEndpointDoc.from_mongo(d) for d in docs]

    async def backlog_totals(self) -> tuple[int, int]:
        """(endpoints with pending rows, pending rows across all of them)."""
        cursor = await self._col.aggregate(
            [
                {"$match": {"pending_count": {"$gt": 0}}},
                {
                    "$group": {
                        "_id": None,
                        "endpoints": {"$sum": 1},
                        "total": {"$sum": "$pending_count"},
                    }
                },
            ]
        )
        rows = await cursor.to_list(length=1)
        if not rows:
            return 0, 0
        return int(rows[0]["endpoints"]), int(rows[0]["total"])

    async def find_backlogged(self, *, limit: int) -> list[WebhookEndpointDoc]:
        docs = (
            await self._col.find({"pending_count": {"$gt": 0}})
            .sort("pending_count", -1)
            .limit(limit)
            .to_list(length=limit)
        )
        return [WebhookEndpointDoc.from_mongo(d) for d in docs]

    async def count_by_user(self, user_id: ObjectId) -> int:
        return await self._count({"user_id": user_id})

    async def count_active_for_owner(self, owner_id: ObjectId) -> int:
        return await self._count(
            {"user_id": owner_id, "status": WebhookStatus.ACTIVE.value}
        )

    async def update_fields(self, endpoint_id: ObjectId, fields: dict) -> bool:
        fields["updated_at"] = datetime.now(timezone.utc)
        return await self._update({"_id": endpoint_id}, {"$set": fields})

    async def delete_endpoint(self, endpoint_id: ObjectId, user_id: ObjectId) -> bool:
        return await self._delete({"_id": endpoint_id, "user_id": user_id})

    async def delete_by_user(self, user_id: ObjectId) -> int:
        """Delete every endpoint the user owns (account erasure).

        Returns the number of documents deleted.
        """
        return await self._delete_many({"user_id": user_id})

    # ── Health counters (executor/dispatcher side) ───────────────────────

    async def record_success(self, endpoint_id: ObjectId) -> int:
        """Reset failure streak, drain dropped_count. Returns the drained
        dropped_count so the NEXT delivery can carry ``dropped_since_last``."""
        now = datetime.now(timezone.utc)
        doc = await self._col.find_one_and_update(
            {"_id": endpoint_id},
            {
                "$set": {
                    "consecutive_failures": 0,
                    "dropped_count": 0,
                    "last_delivery_at": now,
                    "last_success_at": now,
                },
                "$inc": {"total_successes": 1},
            },
            projection={"dropped_count": 1},
        )
        return int(doc.get("dropped_count", 0)) if doc else 0

    async def record_exhausted(self, endpoint_id: ObjectId, reason: str) -> int:
        """One delivery ran out of retries. Returns the new streak length."""
        doc = await self._col.find_one_and_update(
            {"_id": endpoint_id},
            {
                "$set": {
                    "last_delivery_at": datetime.now(timezone.utc),
                    "last_failure_reason": reason,
                },
                "$inc": {"consecutive_failures": 1},
            },
            return_document=True,
        )
        return int(doc.get("consecutive_failures", 0)) if doc else 0

    async def reserve_pending(self, endpoint_id: ObjectId, cap: int) -> bool:
        """Take one pending slot under ``cap``; False when the endpoint is
        at the cap (or the counter field is missing, which the caller
        repairs). ``total_deliveries`` is counted at ENQUEUE, not at
        terminal state — "0 of 2" while two deliveries are mid-ladder beats
        a "0 of 0" that contradicts the visible delivery log."""
        return await self._update(
            {"_id": endpoint_id, "pending_count": {"$lt": cap}},
            {"$inc": {"pending_count": 1, "total_deliveries": 1}},
        )

    async def release_pending(self, endpoint_id: ObjectId) -> None:
        await self._update(
            {"_id": endpoint_id, "pending_count": {"$gt": 0}},
            {"$inc": {"pending_count": -1}},
        )

    async def set_pending_count(self, endpoint_id: ObjectId, count: int) -> int:
        """Overwrite the counter with a real count; returns the value it
        replaced (0 when the field was missing)."""
        doc = await self._col.find_one_and_update(
            {"_id": endpoint_id},
            {"$set": {"pending_count": count}},
            projection={"pending_count": 1},
        )
        return int(doc.get("pending_count", 0)) if doc else 0

    async def increment_dropped(self, endpoint_id: ObjectId) -> None:
        await self._update({"_id": endpoint_id}, {"$inc": {"dropped_count": 1}})

    async def reactivate_over_limit(self, endpoint_id: ObjectId) -> bool:
        """Lift an over-limit pause; any other disabled reason stays."""
        return await self._update(
            {
                "_id": endpoint_id,
                "disabled_reason": EndpointDisabledReason.OVER_LIMIT.value,
            },
            {
                "$set": {
                    "status": WebhookStatus.ACTIVE.value,
                    "disabled_reason": None,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )

    async def disable(
        self, endpoint_id: ObjectId, reason: EndpointDisabledReason
    ) -> bool:
        return await self._update(
            {"_id": endpoint_id},
            {
                "$set": {
                    "status": WebhookStatus.DISABLED.value,
                    "disabled_reason": reason.value,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
