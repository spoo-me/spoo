"""Unit tests for CreationPatternScorer (L1)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.safety.scoring import CreationPatternScorer


def _redis(counts: list[int]) -> MagicMock:
    """Fake redis whose pipeline returns interleaved incr/expire results
    for however many keys the scorer bumped."""
    redis = MagicMock()
    pipe = MagicMock()
    results: list = []
    for c in counts:
        results.extend([c, True])
    pipe.execute = AsyncMock(return_value=results)
    redis.pipeline = MagicMock(return_value=pipe)
    # The fired marker: SET NX succeeds by default (first crossing).
    redis.set = AsyncMock(return_value=True)
    return redis, pipe


def _scorer(redis, *, sink=None, notifier=None, **thresholds):
    defaults = dict(
        burst_window_seconds=600,
        domain_burst_threshold=3,
        domain_daily_threshold=10,
    )
    defaults.update(thresholds)
    return CreationPatternScorer(
        redis, sink or AsyncMock(), notifier or AsyncMock(), **defaults
    )


class TestCounters:
    @pytest.mark.asyncio
    async def test_bumps_both_domain_windows(self):
        redis, pipe = _redis([1, 1])
        scorer = _scorer(redis)

        await scorer.record_create("https://a.evil.com/x", "a.evil.com", "evil.com")

        keys = [call.args[0] for call in pipe.incr.call_args_list]
        assert len(keys) == 2
        assert keys[0].startswith("l1:dom:600:evil.com:")
        assert keys[1].startswith("l1:dom:86400:evil.com:")

    @pytest.mark.asyncio
    async def test_no_redis_is_a_noop(self):
        sink = AsyncMock()
        scorer = _scorer(None, sink=sink)
        await scorer.record_create("https://a.com/x", "a.com", "a.com")
        sink.emit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_redis_failure_never_raises(self):
        redis = MagicMock()
        redis.pipeline = MagicMock(side_effect=RuntimeError("redis down"))
        await _scorer(redis).record_create("https://a.com/x", "a.com", "a.com")


class TestThresholds:
    @pytest.mark.asyncio
    async def test_domain_burst_crossing_enqueues_analysis(self):
        sink = AsyncMock()
        redis, _ = _redis([3, 5])  # burst hits threshold exactly
        await _scorer(redis, sink=sink).record_create(
            "https://a.evil.com/x", "a.evil.com", "evil.com"
        )
        event = sink.emit.await_args.args[0]
        assert event.trigger == "pattern"
        assert event.host == "a.evil.com"
        assert event.registrable_domain == "evil.com"
        assert event.context == {"window_seconds": 600, "creates": 3}

    @pytest.mark.asyncio
    async def test_fires_once_per_window(self):
        """Marker already taken means the window already fired: no event storm."""
        sink = AsyncMock()
        redis, _ = _redis([4, 5])  # burst already past threshold
        redis.set = AsyncMock(return_value=None)  # NX lost: already fired
        await _scorer(redis, sink=sink).record_create(
            "https://a.evil.com/x", "a.evil.com", "evil.com"
        )
        sink.emit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_counter_jump_past_threshold_still_fires(self):
        """Double-applied INCRs skip exact equality; the marker still fires."""
        sink = AsyncMock()
        redis, _ = _redis([5, 1])  # jumped from 3 straight to 5
        await _scorer(redis, sink=sink).record_create(
            "https://a.evil.com/x", "a.evil.com", "evil.com"
        )
        sink.emit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_daily_domain_crossing_enqueues_analysis(self):
        sink = AsyncMock()
        redis, _ = _redis([1, 10])
        await _scorer(redis, sink=sink).record_create(
            "https://a.evil.com/x", "a.evil.com", "evil.com"
        )
        assert sink.emit.await_args.args[0].context["window_seconds"] == 86_400
