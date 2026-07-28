"""Dead-letter guard for the claim path of click stream consumer groups.

FastStream's claimer subscriber (``min_idle_time``) retries stuck messages
forever — a poison message (one that always fails) would loop between
claim → fail → idle → claim. This guard runs at the START of the claim
path: it checks the message's server-side delivery counter (``XPENDING``)
and, past ``max_deliveries``, moves the payload to the DLQ stream instead
of processing it. Returning normally afterwards lets FastStream XACK the
original, ending the loop.

New (first-delivery) messages never pass through here — the guard costs
one extra Redis round-trip only on the rare recovery path.
"""

from __future__ import annotations

import json
from typing import Any

from infrastructure.logging import get_logger
from services.click.events import STREAM_FIELD_DATA

log = get_logger(__name__)

DLQ_FIELD_SOURCE_ID = "dlq_source_id"
DLQ_FIELD_GROUP = "dlq_group"
DLQ_FIELD_REASON = "dlq_reason"
_REASON_MAX_DELIVERIES = "max_deliveries_exceeded"

# Queue Redis runs noeviction: an unbounded DLQ could fill memory and start
# rejecting ALL writes, including the main click stream. Any real backlog is
# orders of magnitude smaller than this bound.
DLQ_MAXLEN = 10_000


class ClaimDeadLetterGuard:
    """Decides whether a claimed message should be dead-lettered."""

    def __init__(
        self,
        stream: str,
        group: str,
        dlq_stream: str,
        max_deliveries: int,
        dlq_maxlen: int = DLQ_MAXLEN,
    ) -> None:
        self._stream = stream
        self._group = group
        self._dlq_stream = dlq_stream
        self._max_deliveries = max_deliveries
        self._dlq_maxlen = dlq_maxlen

    async def intercept(self, redis: Any, message_id: str, payload: Any) -> bool:
        """Return True when the message was dead-lettered (skip processing).

        Fails open: if the delivery count can't be read or the DLQ write
        fails, the message is processed normally — one more attempt is
        always safer than losing the event.
        """
        deliveries = await self._times_delivered(redis, message_id)
        if deliveries is None or deliveries <= self._max_deliveries:
            return False

        try:
            await redis.xadd(
                self._dlq_stream,
                {
                    STREAM_FIELD_DATA: json.dumps(payload, default=str),
                    DLQ_FIELD_SOURCE_ID: message_id,
                    DLQ_FIELD_GROUP: self._group,
                    DLQ_FIELD_REASON: _REASON_MAX_DELIVERIES,
                },
                maxlen=self._dlq_maxlen,
                approximate=True,
            )
        except Exception as exc:
            log.error(
                "click_event_dlq_write_failed",
                message_id=message_id,
                group=self._group,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return False

        log.error(
            "click_event_dead_lettered",
            message_id=message_id,
            group=self._group,
            deliveries=deliveries,
            dlq_stream=self._dlq_stream,
        )
        return True

    async def _times_delivered(self, redis: Any, message_id: str) -> int | None:
        try:
            pending = await redis.xpending_range(
                self._stream,
                self._group,
                min=message_id,
                max=message_id,
                count=1,
            )
        except Exception as exc:
            log.warning(
                "click_event_pending_lookup_failed",
                message_id=message_id,
                group=self._group,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return None
        if not pending:
            return None
        entry = pending[0]
        times = entry.get("times_delivered")
        return int(times) if times is not None else None
