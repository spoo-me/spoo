"""Tests for click event sinks."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services.click.events import (
    EVENT_TYPE_CLICK,
    STREAM_FIELD_DATA,
    STREAM_FIELD_TYPE,
    ClickEvent,
)
from app.services.click.sinks import InlineSink, RedisStreamSink
from tests.factories import make_click_event as make_event


def assert_track_click_matches_event(
    click_service: AsyncMock, event: ClickEvent
) -> None:
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
        clicked_at=event.enqueued_at,
    )


class TestInlineSink:
    async def test_replays_event_through_click_service(self):
        click_service = AsyncMock()
        sink = InlineSink(click_service)
        event = make_event()

        await sink.emit(event)

        assert_track_click_matches_event(click_service, event)

    async def test_exceptions_propagate_to_caller(self):
        """The route owns error semantics (ValidationError/ForbiddenError)."""
        click_service = AsyncMock()
        click_service.track_click.side_effect = RuntimeError("boom")
        sink = InlineSink(click_service)

        with pytest.raises(RuntimeError):
            await sink.emit(make_event())


class TestRedisStreamSink:
    async def test_xadds_encoded_event_with_maxlen(self):
        redis = AsyncMock()
        fallback = AsyncMock()
        sink = RedisStreamSink(
            redis, stream="events:clicks", maxlen=1000, fallback=fallback
        )
        event = make_event()

        await sink.emit(event)

        redis.xadd.assert_awaited_once()
        args, kwargs = redis.xadd.await_args
        assert args[0] == "events:clicks"
        fields = args[1]
        assert fields[STREAM_FIELD_TYPE] == EVENT_TYPE_CLICK
        assert STREAM_FIELD_DATA in fields
        assert kwargs["maxlen"] == 1000
        assert kwargs["approximate"] is True
        # Redis 8.2 consumer-group-aware trimming: consumed history
        # self-cleans on write; unacked backlog is never trimmed.
        assert kwargs["ref_policy"] == "ACKED"
        fallback.emit.assert_not_awaited()

    async def test_falls_back_inline_on_xadd_failure(self):
        redis = AsyncMock()
        redis.xadd.side_effect = ConnectionError("queue redis down")
        fallback = AsyncMock()
        sink = RedisStreamSink(
            redis, stream="events:clicks", maxlen=1000, fallback=fallback
        )
        event = make_event()

        await sink.emit(event)

        fallback.emit.assert_awaited_once_with(event)

    async def test_falls_back_on_full_stream_noeviction_error(self):
        """noeviction queue Redis rejects XADD with OOM — must degrade, not fail."""
        redis = AsyncMock()
        redis.xadd.side_effect = Exception(
            "OOM command not allowed when used memory > 'maxmemory'"
        )
        fallback = AsyncMock()
        sink = RedisStreamSink(
            redis, stream="events:clicks", maxlen=1000, fallback=fallback
        )

        await sink.emit(make_event())

        fallback.emit.assert_awaited_once()

    async def test_fallback_exceptions_propagate(self):
        """ForbiddenError from the inline fallback must reach the route."""
        redis = AsyncMock()
        redis.xadd.side_effect = ConnectionError("down")
        fallback = AsyncMock()
        fallback.emit.side_effect = RuntimeError("inline failed")
        sink = RedisStreamSink(
            redis, stream="events:clicks", maxlen=1000, fallback=fallback
        )

        with pytest.raises(RuntimeError):
            await sink.emit(make_event())
