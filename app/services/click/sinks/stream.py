"""RedisStreamSink — fire-and-forget click emission onto a Redis Stream."""

from __future__ import annotations

import redis.asyncio as aioredis

from app.infrastructure.logging import get_logger
from app.services.click.events import ClickEvent, to_stream_fields
from app.services.click.sinks.protocol import ClickEventSink

log = get_logger(__name__)


class RedisStreamSink:
    """XADDs encoded events to the click stream (~1 ms on the hot path).

    On ANY XADD failure (queue Redis down, stream full under noeviction,
    network blip) the event is processed through the inline fallback —
    the site degrades to synchronous tracking instead of losing the click
    or failing the redirect. The fallback is surfaced via the
    ``click_sink_fallback`` log event so it stays alertable.
    """

    def __init__(
        self,
        redis_client: aioredis.Redis,
        stream: str,
        maxlen: int,
        fallback: ClickEventSink,
    ) -> None:
        self._redis = redis_client
        self._stream = stream
        self._maxlen = maxlen
        self._fallback = fallback

    async def emit(self, event: ClickEvent) -> None:
        try:
            # ref_policy="ACKED" (Redis >= 8.2): each XADD also sweeps entries
            # beyond `maxlen` that EVERY consumer group has acknowledged —
            # consumed history self-cleans on write, while unacked backlog is
            # untrimmable regardless of size (never lose a click).
            await self._redis.xadd(
                self._stream,
                to_stream_fields(event),
                maxlen=self._maxlen,
                approximate=True,
                ref_policy="ACKED",
            )
        except Exception as exc:
            log.warning(
                "click_sink_fallback",
                short_code=event.short_code,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            await self._fallback.emit(event)
