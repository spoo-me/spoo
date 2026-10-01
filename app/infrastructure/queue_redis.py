"""Click-events queue Redis connection with the 8.2 capability gate.

Stream mode publishes with ``XADD ... ACKED`` (consumer-group-aware
trimming, Redis >= 8.2). Older servers would reject every emit, so the
version is gated once at connect with a clear operator message; callers
receive ``None`` and the click sink degrades to inline tracking.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import redis.asyncio as aioredis

from app.infrastructure.cache.redis_client import create_redis_client
from app.infrastructure.logging import get_logger

if TYPE_CHECKING:
    from app.config import ClickEventsSettings

log = get_logger(__name__)

MIN_QUEUE_REDIS_VERSION = (8, 2)


def parse_redis_version(version: str) -> tuple[int, int] | None:
    """``"8.2.1"`` -> ``(8, 2)``; None when unparseable."""
    try:
        major, minor, *_ = (int(part) for part in version.split(".")[:2])
    except (ValueError, AttributeError):
        return None
    return (major, minor)


async def connect_queue_redis(
    settings: ClickEventsSettings,
) -> aioredis.Redis | None:
    """Connect to the queue Redis when a URI is configured, or return None.

    None when: no URI, the server is unreachable, or the server predates
    ACKED trimming. Each consumer of the connection decides for itself
    (click-sink wiring checks ``sink == "stream"``; the meta-image sink
    checks its own toggle) — inline-click deployments can still run async
    meta-image validation.
    """
    if not settings.queue_redis_uri:
        return None

    client = await create_redis_client(
        settings.queue_redis_uri, label="click-events-queue"
    )
    if client is None:
        return None

    version = "unknown"
    try:
        info = await client.info("server")
        version = info.get("redis_version", "unknown")
        parsed = parse_redis_version(version)
        supported = parsed is not None and parsed >= MIN_QUEUE_REDIS_VERSION
    except Exception:
        supported = False

    if not supported:
        log.error(
            "queue_redis_version_unsupported",
            version=version,
            detail=(
                "Click event streaming requires Redis >= "
                f"{'.'.join(map(str, MIN_QUEUE_REDIS_VERSION))} "
                "(XADD ACKED trimming). Falling back to inline click tracking."
            ),
        )
        await client.aclose()
        return None

    return client
