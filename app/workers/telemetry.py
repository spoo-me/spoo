"""Worker telemetry loops: stream metrics + stale consumer cleanup.

StreamMetricsReporter emits a periodic ``click_stream_stats`` log line
(backlog + per-group pending/lag) — the pipeline's health signal, shipped
to Axiom via the existing container-logs → Vector path, and the thing to
alert on (lag growing = worker falling behind; alert BEFORE the buffer
fills and the sink starts falling back inline).

WebhookDepthReporter emits per-endpoint pending depth from the counter the
dispatcher maintains — the queue-side view of a subscriber falling behind.

StaleConsumerJanitor deletes consumer names that are long-dead with
nothing pending — every worker restart registers fresh
``{group}-{host}-{pid}`` names and Redis keeps the old ones forever,
cluttering ``XINFO CONSUMERS``. Deleting a consumer with pending
messages would orphan its PEL entries, so only pending==0 names go.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from app.infrastructure.logging import get_logger

if TYPE_CHECKING:
    from app.repositories.webhook_endpoint_repository import WebhookEndpointRepository

log = get_logger(__name__)

# A consumer idle > 30 min with nothing pending is a restart leftover:
# live consumers block-read every ~2s, so real idle times stay in seconds.
STALE_CONSUMER_IDLE_MS = 30 * 60 * 1000
CONSUMER_GC_INTERVAL_SECONDS = 15 * 60


class StreamMetricsReporter:
    def __init__(self, redis_client: Any, stream: str, interval_seconds: float) -> None:
        self._redis = redis_client
        self._stream = stream
        self._interval = interval_seconds

    async def run_forever(self) -> None:
        while True:
            try:
                await self.report_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "click_stream_stats_failed",
                    stream=self._stream,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
            await asyncio.sleep(self._interval)

    async def report_once(self) -> None:
        backlog = await self._redis.xlen(self._stream)
        groups = await self._redis.xinfo_groups(self._stream)
        log.info(
            "click_stream_stats",
            stream=self._stream,
            backlog=backlog,
            groups=[
                {
                    "name": g.get("name"),
                    "pending": g.get("pending"),
                    "lag": g.get("lag"),
                }
                for g in groups
            ],
        )
        # One flat line per group so lag can be charted and alerted on by
        # group without unpacking the nested list above.
        for g in groups:
            log.info(
                "stream_group_stats",
                stream=self._stream,
                group=g.get("name"),
                pending=g.get("pending"),
                lag=g.get("lag"),
            )


class StaleConsumerJanitor:
    def __init__(
        self,
        redis_client: Any,
        stream: str,
        idle_threshold_ms: int = STALE_CONSUMER_IDLE_MS,
        interval_seconds: float = CONSUMER_GC_INTERVAL_SECONDS,
    ) -> None:
        self._redis = redis_client
        self._stream = stream
        self._idle_threshold_ms = idle_threshold_ms
        self._interval = interval_seconds

    async def run_forever(self) -> None:
        while True:
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "stale_consumer_sweep_failed",
                    stream=self._stream,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
            await asyncio.sleep(self._interval)

    async def sweep_once(self) -> int:
        """Delete dead consumer names; returns how many were removed."""
        removed = 0
        for group in await self._redis.xinfo_groups(self._stream):
            group_name = group.get("name")
            for consumer in await self._redis.xinfo_consumers(self._stream, group_name):
                if (
                    consumer.get("pending", 0) == 0
                    and consumer.get("idle", 0) > self._idle_threshold_ms
                ):
                    await self._redis.xgroup_delconsumer(
                        self._stream, group_name, consumer["name"]
                    )
                    removed += 1
        if removed:
            log.info("stale_consumers_removed", stream=self._stream, removed=removed)
        return removed


class WebhookDepthReporter:
    def __init__(
        self,
        endpoint_repo: WebhookEndpointRepository,
        interval_seconds: float,
        top: int = 20,
    ) -> None:
        self._endpoints = endpoint_repo
        self._interval = interval_seconds
        self._top = top

    async def run_forever(self) -> None:
        while True:
            try:
                await self.report_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "webhook_pending_depth_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
            await asyncio.sleep(self._interval)

    async def report_once(self) -> None:
        endpoints, total = await self._endpoints.backlog_totals()
        backlogged = await self._endpoints.find_backlogged(limit=self._top)
        log.info(
            "webhook_pending_depth",
            backlogged_endpoints=endpoints,
            total_pending=total,
            max_pending=backlogged[0].pending_count if backlogged else 0,
        )
        for ep in backlogged:
            log.info(
                "webhook_endpoint_depth",
                endpoint_id=str(ep.id),
                user_id=str(ep.user_id),
                pending=ep.pending_count,
                status=ep.status.value,
            )
