"""Tests for the click stream consumer classes (framework-free logic)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.errors import ForbiddenError, ValidationError
from app.services.click.consumers import (
    HotUrl,
    HotUrlDetector,
    LogHotUrlAction,
    StatsClickConsumer,
)
from tests.factories import make_click_event as make_event


def _payload(**overrides) -> dict[str, Any]:
    """A click event as FastStream delivers it: the decoded __data__ dict."""
    return make_event(**overrides).model_dump(mode="json")


# ── StatsClickConsumer ────────────────────────────────────────────────────────


class TestStatsClickConsumer:
    async def test_replays_event_through_click_service(self):
        click_service = AsyncMock()
        consumer = StatsClickConsumer(click_service)
        event = make_event()

        await consumer.consume(event.model_dump(mode="json"))

        click_service.track_click.assert_awaited_once_with(
            url_data=event.url,
            short_code=event.short_code,
            schema=event.schema_key,
            is_emoji=event.is_emoji,
            client_ip=event.client_ip,
            redirect_ms=event.redirect_ms,
            user_agent=event.user_agent,
            referrer=event.referrer,
            cf_city=event.cf_city,
            utm_source=event.utm_source,
            utm_medium=event.utm_medium,
            utm_campaign=event.utm_campaign,
            variant_index=event.variant_index,
        )

    async def test_drops_undecodable_payload_without_raising(self):
        """Malformed payloads can never succeed — drop, don't poison the group."""
        click_service = AsyncMock()
        consumer = StatsClickConsumer(click_service)

        await consumer.consume("not a dict")
        await consumer.consume(None)
        await consumer.consume({"short_code": "abc"})  # missing required fields

        click_service.track_click.assert_not_awaited()

    async def test_swallows_permanent_validation_error(self):
        """Bad UA is terminal — same outcome as inline mode, no retry."""
        click_service = AsyncMock()
        click_service.track_click.side_effect = ValidationError("bad UA")
        consumer = StatsClickConsumer(click_service)

        await consumer.consume(_payload())  # must not raise

    async def test_swallows_forbidden_error(self):
        """v1 bot with block_bots: the redirect was already served; recording
        nothing matches inline behavior. No retry."""
        click_service = AsyncMock()
        click_service.track_click.side_effect = ForbiddenError("bot")
        consumer = StatsClickConsumer(click_service)

        await consumer.consume(_payload())  # must not raise

    async def test_raises_transient_errors_for_redelivery(self):
        """Mongo/GeoIP blips must propagate so the message stays pending."""
        click_service = AsyncMock()
        click_service.track_click.side_effect = ConnectionError("mongo down")
        consumer = StatsClickConsumer(click_service)

        with pytest.raises(ConnectionError):
            await consumer.consume(_payload())


# ── HotUrlDetector ────────────────────────────────────────────────────────────


def _mock_redis_with_count(count: int) -> tuple[MagicMock, MagicMock]:
    """Redis mock whose pipeline execute() reports `count` after INCR."""
    redis = MagicMock()
    pipe = MagicMock()
    pipe.incr = MagicMock(return_value=pipe)
    pipe.expire = MagicMock(return_value=pipe)
    pipe.execute = AsyncMock(return_value=[count, True])
    redis.pipeline = MagicMock(return_value=pipe)
    return redis, pipe


class TestHotUrlDetector:
    async def test_counts_in_domain_scoped_window_key(self):
        redis, pipe = _mock_redis_with_count(1)
        detector = HotUrlDetector(redis, threshold=50, window_seconds=60, actions=[])

        await detector.consume(_payload())

        key = pipe.incr.call_args.args[0]
        assert key.startswith("hot:spoo.me:abc:")
        window_bucket = int(key.rsplit(":", 1)[1])
        assert window_bucket > 0
        pipe.expire.assert_called_once_with(key, 120)  # 2x window

    async def test_uses_default_domain_when_event_domain_empty(self):
        redis, pipe = _mock_redis_with_count(1)
        detector = HotUrlDetector(redis, threshold=50, window_seconds=60, actions=[])

        payload = make_event(
            url=make_event().url.model_copy(update={"domain": ""})
        ).model_dump(mode="json")
        await detector.consume(payload)

        assert pipe.incr.call_args.args[0].startswith("hot:default:abc:")

    async def test_fires_actions_exactly_at_threshold(self):
        redis, _ = _mock_redis_with_count(50)
        action = AsyncMock()
        detector = HotUrlDetector(
            redis, threshold=50, window_seconds=60, actions=[action]
        )

        await detector.consume(_payload())

        action.on_hot.assert_awaited_once()
        hot = action.on_hot.await_args.args[0]
        assert isinstance(hot, HotUrl)
        assert hot.short_code == "abc"
        assert hot.domain == "spoo.me"
        assert hot.count == 50

    async def test_does_not_fire_below_threshold(self):
        redis, _ = _mock_redis_with_count(49)
        action = AsyncMock()
        detector = HotUrlDetector(
            redis, threshold=50, window_seconds=60, actions=[action]
        )

        await detector.consume(_payload())

        action.on_hot.assert_not_awaited()

    async def test_does_not_refire_past_threshold(self):
        """Fires once per window — count 51+ stays silent until re-promotion."""
        redis, _ = _mock_redis_with_count(51)
        action = AsyncMock()
        detector = HotUrlDetector(
            redis, threshold=50, window_seconds=60, actions=[action]
        )

        await detector.consume(_payload())

        action.on_hot.assert_not_awaited()

    async def test_all_actions_fire_and_failures_are_isolated(self):
        """One broken action must not stop the others, and must never raise
        (a failed side effect must not trigger click redelivery)."""
        redis, _ = _mock_redis_with_count(50)
        broken = AsyncMock()
        broken.on_hot.side_effect = RuntimeError("cf api down")
        healthy = AsyncMock()
        detector = HotUrlDetector(
            redis, threshold=50, window_seconds=60, actions=[broken, healthy]
        )

        await detector.consume(_payload())  # must not raise

        broken.on_hot.assert_awaited_once()
        healthy.on_hot.assert_awaited_once()

    async def test_drops_undecodable_payload(self):
        redis, pipe = _mock_redis_with_count(1)
        detector = HotUrlDetector(redis, threshold=50, window_seconds=60, actions=[])

        await detector.consume("{broken")

        pipe.incr.assert_not_called()

    async def test_redis_counter_failure_does_not_raise(self):
        """Hotness is best-effort — a queue Redis blip must not redeliver
        the click (stats already processed it independently)."""
        redis = MagicMock()
        pipe = MagicMock()
        pipe.incr = MagicMock(return_value=pipe)
        pipe.expire = MagicMock(return_value=pipe)
        pipe.execute = AsyncMock(side_effect=ConnectionError("redis blip"))
        redis.pipeline = MagicMock(return_value=pipe)
        detector = HotUrlDetector(redis, threshold=50, window_seconds=60, actions=[])

        await detector.consume(_payload())  # must not raise


class TestLogHotUrlAction:
    async def test_logs_without_error(self):
        action = LogHotUrlAction()
        await action.on_hot(
            HotUrl(domain="spoo.me", short_code="abc", count=50, window_bucket=1)
        )
