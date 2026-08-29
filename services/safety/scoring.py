"""CreationPatternScorer — L1: the signal no external feed can see.

Fixed-window Redis counters over link CREATION (the hotness detector's
math pointed at creates instead of clicks): creates per destination
registrable domain, at a burst window (minutes) and a daily window. A
counter crossing its threshold pushes the destination into the same
analyzer the reports feed (`trigger="pattern"`).

There is deliberately NO per-IP counter: the rate limiter already bounds
per-IP volume and an IP is a terrible creator identity (CGNAT/VPNs).

Counters live on the QUEUE Redis (noeviction): the cache Redis runs
allkeys-lru and would silently evict them. No queue Redis means scoring
is a no-op — pattern detection is an enhancement, never a dependency.
Everything here is best-effort by contract: record_create never raises
and never blocks a write.

Thresholds are private tuning (SAFETY_L1_* env vars, never committed):
calibrate by replaying past campaigns against candidate values.
"""

from __future__ import annotations

import time

from infrastructure.logging import get_logger
from infrastructure.ops_notify import OpsNotifier
from services.safety.events import SafetyAnalyzeEvent
from services.safety.sinks import SafetySink

log = get_logger(__name__)

_DAILY_WINDOW = 86_400


class CreationPatternScorer:
    def __init__(
        self,
        redis_client,
        sink: SafetySink,
        notifier: OpsNotifier,
        *,
        burst_window_seconds: int = 600,
        domain_burst_threshold: int = 50,
        domain_daily_threshold: int = 300,
    ) -> None:
        self._redis = redis_client
        self._sink = sink
        self._notifier = notifier
        self._burst_window = burst_window_seconds
        self._thresholds = {
            (burst_window_seconds): domain_burst_threshold,
            (_DAILY_WINDOW): domain_daily_threshold,
        }

    async def record_create(
        self,
        url: str,
        host: str,
        registrable_domain: str,
    ) -> None:
        """Count one successful create; act on any threshold crossing."""
        if self._redis is None or not registrable_domain:
            return
        try:
            counts = await self._bump(registrable_domain)
        except Exception as exc:
            log.warning(
                "l1_scoring_failed", error=str(exc), error_type=type(exc).__name__
            )
            return
        for window, count in counts.items():
            # Each window fires once; a sustained campaign re-fires each new window.
            if count < self._thresholds[window]:
                continue
            if not await self._mark_fired(window, registrable_domain):
                continue
            await self._on_domain_burst(url, host, registrable_domain, window, count)

    async def _mark_fired(self, window: int, registrable_domain: str) -> bool:
        """SET NX fire-once that survives redis-py retries: a retried pipeline
        re-applies the INCRs, jumping the counter past exact equality."""
        bucket = int(time.time()) // window
        key = f"l1:fired:dom:{window}:{registrable_domain}:{bucket}"
        try:
            return bool(await self._redis.set(key, "1", nx=True, ex=window * 2))
        except Exception as exc:
            log.warning(
                "l1_fired_marker_failed", error=str(exc), error_type=type(exc).__name__
            )
            return False

    async def _bump(self, registrable_domain: str) -> dict[int, int]:
        now = int(time.time())
        keys: list[tuple[int, str]] = []
        for window in (self._burst_window, _DAILY_WINDOW):
            bucket = now // window
            keys.append((window, f"l1:dom:{window}:{registrable_domain}:{bucket}"))
        pipe = self._redis.pipeline(transaction=False)
        for window, key in keys:
            pipe.incr(key)
            # 2x window: inspectable after it closes, self-decaying.
            pipe.expire(key, window * 2)
        results = await pipe.execute()
        return {window: int(results[i * 2]) for i, (window, _) in enumerate(keys)}

    async def _on_domain_burst(
        self, url: str, host: str, registrable_domain: str, window: int, count: int
    ) -> None:
        log.warning(
            "l1_domain_burst",
            registrable_domain=registrable_domain,
            window_seconds=window,
            creates=count,
        )
        # Same pipe as reports: analyzer -> verdict -> enforcement/review.
        await self._sink.emit(
            SafetyAnalyzeEvent(
                url=url,
                host=host,
                registrable_domain=registrable_domain,
                trigger="pattern",
                context={"window_seconds": window, "creates": count},
            )
        )
