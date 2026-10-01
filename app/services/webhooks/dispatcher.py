"""WebhookDispatcher — fact → durable delivery intent. No HTTP, no rendering.

Runs on the consumer ack path (stream mode) or at emit time (inline
mode), so its cost must be bounded and payload-size-independent: one
matcher pass, one event insert, N thin delivery rows. The
caller acks the stream message AFTER dispatch returns — delivery intent
recorded durably is the ack condition, not delivery success — a
multi-hour retry ladder cannot hold a stream entry pending.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from bson import ObjectId

from app.infrastructure.logging import get_logger
from app.repositories.webhook_delivery_repository import WebhookDeliveryRepository
from app.repositories.webhook_endpoint_repository import WebhookEndpointRepository
from app.repositories.webhook_event_repository import WebhookEventRepository
from app.schemas.enums.webhook import DeliveryStatus
from app.schemas.models.webhook import WebhookEndpointDoc
from app.services.events.contract import DomainEvent
from app.services.webhooks.matcher import SubscriptionMatcher
from app.services.webhooks.signing import new_webhook_id

log = get_logger(__name__)


def make_delivery_row(
    event_oid: ObjectId, event: DomainEvent, endpoint: WebhookEndpointDoc
) -> dict:
    return {
        "endpoint_id": endpoint.id,
        "user_id": endpoint.user_id,
        "event_oid": event_oid,
        "event_type": event.type,
        "webhook_id": new_webhook_id(),
        "is_test": False,
        "rendered_body": None,
        # Carry the drop counter accumulated while this endpoint was over
        # the pending cap; the renderer surfaces it and record_success
        # resets it, so the signal reaches the subscriber on the next
        # delivery that lands. At-least-once: consecutive rows may repeat
        # the same count until one succeeds.
        "dropped_since_last": endpoint.dropped_count,
        "status": DeliveryStatus.PENDING.value,
        "attempts": [],
        "attempt_count": 0,
        "next_attempt_at": datetime.now(timezone.utc),
        "claimed_until": None,
        "created_at": datetime.now(timezone.utc),
        "completed_at": None,
    }


class WebhookDispatcher:
    def __init__(
        self,
        matcher: SubscriptionMatcher,
        event_repo: WebhookEventRepository,
        delivery_repo: WebhookDeliveryRepository,
        endpoint_repo: WebhookEndpointRepository,
        *,
        max_pending_per_endpoint: int = 1000,
        recount_interval_seconds: float = 30.0,
    ) -> None:
        self._matcher = matcher
        self._event_repo = event_repo
        self._delivery_repo = delivery_repo
        self._endpoint_repo = endpoint_repo
        self._max_pending = max_pending_per_endpoint
        self._recount_interval = recount_interval_seconds
        self._recount_not_before: dict[ObjectId, float] = {}

    async def dispatch(self, event: DomainEvent) -> None:
        endpoints = await self._matcher.match(event)
        if not endpoints:
            return  # common case: cache hit, zero further Mongo work

        event_oid = await self._event_repo.insert_event(event)
        rows: list[dict] = []
        for endpoint in endpoints:
            # The pending cap protects the queue itself. A subscriber
            # who can't drink max_pending deliveries has already lost the
            # facts — counting beats pretending.
            if not await self._reserve_slot(endpoint):
                await self._endpoint_repo.increment_dropped(endpoint.id)
                log.warning(
                    "webhook_delivery_dropped_over_cap",
                    endpoint_id=str(endpoint.id),
                    event_type=event.type,
                )
                continue
            rows.append(make_delivery_row(event_oid, event, endpoint))

        await self._delivery_repo.insert_many_rows(rows)
        log.info(
            "webhook_dispatched",
            event_type=event.type,
            event_id=event.event_id,
            endpoints=len(rows),
        )

    async def _reserve_slot(self, endpoint: WebhookEndpointDoc) -> bool:
        """One conditional increment per dispatch; no count in the hot path.

        The counter drifts upward (row insert failed after reserving, TTL
        deleted pending rows), so an over-cap verdict is re-verified with
        one real count before dropping, throttled per endpoint so an
        endpoint truly at the cap does not pay a count per event. A recount
        while sibling dispatches hold uninserted reservations undercounts
        by that many; the cap is a backstop, so that overshoot is accepted.
        """
        if await self._endpoint_repo.reserve_pending(endpoint.id, self._max_pending):
            return True
        now = time.monotonic()
        if now < self._recount_not_before.get(endpoint.id, 0.0):
            return False
        self._recount_not_before[endpoint.id] = now + self._recount_interval
        counted = await self._delivery_repo.count_pending(endpoint.id)
        previous = await self._endpoint_repo.set_pending_count(endpoint.id, counted)
        if previous != counted:
            log.warning(
                "webhook_pending_count_repaired",
                endpoint_id=str(endpoint.id),
                previous=previous,
                counted=counted,
            )
        if counted >= self._max_pending:
            return False
        return await self._endpoint_repo.reserve_pending(endpoint.id, self._max_pending)
