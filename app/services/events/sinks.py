"""DomainEventSink implementations — the OSS opt-in ladder's transport rungs.

StreamDomainEventSink   queue Redis present: XADD to events:domain,
                        inline fallback on ANY failure (mirrors the click
                        RedisStreamSink degradation exactly).
InlineDomainEventSink   no queue Redis: dispatch in-process. Low-volume CRUD
                        events always; click events only reach this via the
                        redirect path's own inline mode, where the deployment
                        is small by definition.
NullDomainEventSink     webhooks disabled: emit is a no-op, producers need
                        no conditionals.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import redis.asyncio as aioredis

from app.infrastructure.logging import get_logger
from app.services.events.contract import (
    DOMAIN_EVENTS_STREAM,
    DomainEvent,
    to_stream_fields,
)

if TYPE_CHECKING:
    from app.services.webhooks.dispatcher import WebhookDispatcher

log = get_logger(__name__)


class NullDomainEventSink:
    async def emit(self, event: DomainEvent) -> None:
        return None


class InlineDomainEventSink:
    """Dispatches in-process — one matcher pass + Mongo writes at emit time.

    Never raises: a webhook side effect must not fail the primary action.
    """

    def __init__(self, dispatcher: WebhookDispatcher) -> None:
        self._dispatcher = dispatcher

    async def emit(self, event: DomainEvent) -> None:
        try:
            await self._dispatcher.dispatch(event)
        except Exception as exc:
            log.error(
                "domain_event_inline_dispatch_failed",
                event_type=event.type,
                event_id=event.event_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )


class StreamDomainEventSink:
    """XADDs encoded events to events:domain; falls back inline on failure."""

    def __init__(
        self,
        redis_client: aioredis.Redis,
        fallback: InlineDomainEventSink | NullDomainEventSink,
        stream: str = DOMAIN_EVENTS_STREAM,
        maxlen: int = 100_000,
    ) -> None:
        self._redis = redis_client
        self._fallback = fallback
        self._stream = stream
        self._maxlen = maxlen

    async def emit(self, event: DomainEvent) -> None:
        try:
            await self._redis.xadd(
                self._stream,
                to_stream_fields(event),
                maxlen=self._maxlen,
                approximate=True,
                ref_policy="ACKED",
            )
        except Exception as exc:
            log.warning(
                "domain_event_sink_fallback",
                event_type=event.type,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            await self._fallback.emit(event)
