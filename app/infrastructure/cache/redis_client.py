"""Async Redis connection factory.

Returns an async redis.Redis client, or None if Redis is not configured
or the connection fails. All callers must handle the None case gracefully.
"""

import contextlib

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from app.infrastructure.logging import get_logger

log = get_logger(__name__)


async def create_redis_client(
    redis_uri: str, *, label: str = "redis"
) -> aioredis.Redis | None:
    """Connect to Redis and return a client, or None on failure.

    ``label`` distinguishes multiple instances (cache vs click-event queue)
    in logs.
    """
    client: aioredis.Redis | None = None
    try:
        client = aioredis.from_url(
            redis_uri,
            encoding="utf-8",
            decode_responses=True,
            socket_keepalive=True,
            health_check_interval=30,
            # Bounded timeouts: a wedged-but-open connection must raise (so
            # callers like the click sink can fall back inline) instead of
            # hanging until the kernel TCP timeout.
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        await client.ping()
        # mask credentials
        log.info("redis_connected", label=label, uri=redis_uri.split("@")[-1])
        return client
    except RedisError as e:
        log.warning(
            "redis_connection_failed",
            label=label,
            error=str(e),
            error_type=type(e).__name__,
        )
    except Exception as e:
        log.warning(
            "redis_unexpected_error",
            label=label,
            error=str(e),
            error_type=type(e).__name__,
        )
    # Reaching here means construction or ping failed — release the pool
    # instead of leaking it (from_url doesn't connect, but it builds one).
    if client is not None:
        with contextlib.suppress(Exception):
            await client.aclose()
    return None
