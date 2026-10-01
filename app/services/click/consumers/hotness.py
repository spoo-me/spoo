"""Hotness consumer — detects URLs receiving burst traffic.

Runs in the click worker as consumer group ``hotness``. Fixed-window
counting in the queue Redis: one INCR per event on
``hot:{domain}:{short_code}:{window_bucket}``. When the counter equals
the threshold (exactly — so it fires once per window), every registered
:class:`HotUrlAction` runs. A URL that stays hot re-fires on each new
window, which downstream actions want (e.g. keeping a CF KV cache entry
fresh, re-scanning a sustained attack).

This is the extension seam for the edge-cache and abuse-detection plans:
implement ``HotUrlAction`` and register it in the worker — nothing else
in the pipeline changes.
"""

from __future__ import annotations

from typing import Any

import redis.asyncio as aioredis
from pydantic import BaseModel, ConfigDict
from typing_extensions import Protocol

from app.infrastructure.logging import get_logger
from app.services.click.events import click_event_from_payload

log = get_logger(__name__)


class HotUrl(BaseModel):
    """Immutable fact: a URL crossed the hotness threshold in a window."""

    model_config = ConfigDict(frozen=True)

    domain: str
    short_code: str
    count: int
    window_bucket: int


class HotUrlAction(Protocol):
    """A reaction to a URL becoming hot (promote to edge cache, scan for
    abuse, alert, ...). Implementations are registered in the worker."""

    async def on_hot(self, hot: HotUrl) -> None: ...


class LogHotUrlAction:
    """Default action: make hotness visible in logs (and Axiom)."""

    async def on_hot(self, hot: HotUrl) -> None:
        log.info(
            "click_hot_url_detected",
            domain=hot.domain,
            short_code=hot.short_code,
            count=hot.count,
            window_bucket=hot.window_bucket,
        )


class HotUrlDetector:
    """Best-effort by design: counter failures and action failures are
    logged, never raised — a broken side effect must not cause click
    events to be redelivered (stats consumes them independently)."""

    def __init__(
        self,
        redis_client: aioredis.Redis,
        threshold: int,
        window_seconds: int,
        actions: list[HotUrlAction],
    ) -> None:
        self._redis = redis_client
        self._threshold = threshold
        self._window = window_seconds
        self._actions = actions

    async def consume(self, payload: Any) -> None:
        event = click_event_from_payload(payload)
        if event is None:
            return

        domain = event.url.domain or "default"
        bucket = int(event.enqueued_at.timestamp()) // self._window
        key = f"hot:{domain}:{event.short_code}:{bucket}"

        try:
            pipe = self._redis.pipeline(transaction=False)
            pipe.incr(key)
            # 2x window: the counter survives long enough to be inspected
            # but self-expires — hot state naturally decays with demand.
            pipe.expire(key, self._window * 2)
            count, _ = await pipe.execute()
        except Exception as exc:
            log.warning(
                "hot_url_count_failed",
                short_code=event.short_code,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return

        if count != self._threshold:
            return

        hot = HotUrl(
            domain=domain,
            short_code=event.short_code,
            count=count,
            window_bucket=bucket,
        )
        for action in self._actions:
            try:
                await action.on_hot(hot)
            except Exception:
                log.exception(
                    "hot_url_action_failed",
                    action=type(action).__name__,
                    short_code=hot.short_code,
                )
