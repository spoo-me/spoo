"""Producer sinks for safety analysis events.

Three rungs, mirroring the click pipeline's degradation ladder:

- RedisStreamSafetySink — queue Redis present: the worker analyzes async.
- InlineSafetySink — no queue Redis: analyze in-process, still after the
  trigger's own work is stored (the analyzer is best-effort by contract).
- NullSafetySink — safety disabled: emit is a no-op.

Emit errors are logged and swallowed: a report must store and a link must
redirect regardless of the safety pipeline's health.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from infrastructure.logging import get_logger
from services.safety.analyzer import SafetyAnalyzer
from services.safety.events import SafetyAnalyzeEvent, to_stream_fields

log = get_logger(__name__)


class SafetySink(Protocol):
    async def emit(self, event: SafetyAnalyzeEvent) -> None: ...


class NullSafetySink:
    async def emit(self, event: SafetyAnalyzeEvent) -> None:
        return None


class InlineSafetySink:
    """Runs the analyzer in-process.

    ``background=True`` (the app's no-worker rung) detaches analysis onto
    its own task so a report POST never waits on scans and webhooks; the
    worker's sweeps keep the synchronous form."""

    def __init__(self, analyzer: SafetyAnalyzer, *, background: bool = False) -> None:
        self._analyzer = analyzer
        self._background = background
        # Strong refs: a bare create_task result can be garbage-collected
        # mid-flight.
        self._tasks: set[asyncio.Task] = set()

    async def emit(self, event: SafetyAnalyzeEvent) -> None:
        if not self._background:
            await self._run(event)
            return
        task = asyncio.create_task(self._run(event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, event: SafetyAnalyzeEvent) -> None:
        try:
            await self._analyzer.analyze(event)
        except Exception as exc:
            log.warning(
                "safety_inline_analyze_failed",
                host=event.host,
                error=str(exc),
                error_type=type(exc).__name__,
            )


class RedisStreamSafetySink:
    def __init__(self, redis_client, *, stream: str, maxlen: int) -> None:
        self._redis = redis_client
        self._stream = stream
        self._maxlen = maxlen

    async def emit(self, event: SafetyAnalyzeEvent) -> None:
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
                "safety_emit_failed",
                host=event.host,
                error=str(exc),
                error_type=type(exc).__name__,
            )


# ── Deep tier (investigation) ────────────────────────────────────────────
# The deep queue reuses the SafetyAnalyzeEvent wire format on its own
# stream. There is deliberately NO inline rung: investigation makes
# outbound calls to hostile destinations and must never ride a request —
# without the queue Redis the deep tier is simply off (Null + one boot
# warning), and screening still covers everything.


class DeepAnalysisSink(Protocol):
    async def emit(self, event: SafetyAnalyzeEvent) -> None: ...


class NullDeepAnalysisSink:
    async def emit(self, event: SafetyAnalyzeEvent) -> None:
        return None


class RedisStreamDeepAnalysisSink:
    def __init__(self, redis_client, *, stream: str, maxlen: int) -> None:
        self._redis = redis_client
        self._stream = stream
        self._maxlen = maxlen

    async def emit(self, event: SafetyAnalyzeEvent) -> None:
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
                "safety_deep_emit_failed",
                host=event.host,
                error=str(exc),
                error_type=type(exc).__name__,
            )
