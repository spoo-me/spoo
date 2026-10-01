"""Producer sinks for meta-image validation events.

Unlike clicks there is NO inline fallback: validation is an enhancement —
never worth failing or slowing a user's write. Emit errors are logged and
swallowed; no queue Redis (self-host) means the Null sink and validation
simply doesn't run (the synchronous checks already did).
"""

from __future__ import annotations

from typing import Protocol

from app.infrastructure.logging import get_logger
from app.services.meta_tags.events import MetaImageValidateEvent, to_stream_fields

log = get_logger(__name__)


class MetaImageValidationSink(Protocol):
    async def emit(self, event: MetaImageValidateEvent) -> None: ...


class NullMetaImageSink:
    async def emit(self, event: MetaImageValidateEvent) -> None:
        return None


class RedisStreamMetaImageSink:
    def __init__(self, redis_client, *, stream: str, maxlen: int) -> None:
        self._redis = redis_client
        self._stream = stream
        self._maxlen = maxlen

    async def emit(self, event: MetaImageValidateEvent) -> None:
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
                "meta_image_validate_emit_failed",
                url_id=event.url_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
