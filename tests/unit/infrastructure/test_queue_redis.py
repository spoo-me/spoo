"""Tests for the queue Redis connection gate."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from app.config import ClickEventsSettings
from app.infrastructure.queue_redis import connect_queue_redis, parse_redis_version


class TestParseRedisVersion:
    def test_standard_versions(self):
        assert parse_redis_version("8.2.1") == (8, 2)
        assert parse_redis_version("7.4.0") == (7, 4)
        assert parse_redis_version("8.8.0") == (8, 8)

    def test_short_version(self):
        assert parse_redis_version("8.2") == (8, 2)

    def test_garbage_returns_none(self):
        assert parse_redis_version("valkey") is None
        assert parse_redis_version("") is None
        assert parse_redis_version(None) is None  # type: ignore[arg-type]


class TestConnectQueueRedis:
    async def test_inline_mode_never_connects(self):
        with patch("app.infrastructure.queue_redis.create_redis_client") as create:
            result = await connect_queue_redis(ClickEventsSettings(sink="inline"))
        assert result is None
        create.assert_not_called()

    async def test_stream_without_uri_returns_none(self):
        result = await connect_queue_redis(ClickEventsSettings(sink="stream"))
        assert result is None

    async def test_unreachable_server_returns_none(self):
        settings = ClickEventsSettings(sink="stream", queue_redis_uri="redis://q/0")
        with patch(
            "app.infrastructure.queue_redis.create_redis_client",
            AsyncMock(return_value=None),
        ):
            assert await connect_queue_redis(settings) is None

    async def test_supported_version_returns_client(self):
        client = AsyncMock()
        client.info.return_value = {"redis_version": "8.2.0"}
        settings = ClickEventsSettings(sink="stream", queue_redis_uri="redis://q/0")
        with patch(
            "app.infrastructure.queue_redis.create_redis_client",
            AsyncMock(return_value=client),
        ):
            assert await connect_queue_redis(settings) is client
        client.aclose.assert_not_awaited()

    async def test_old_server_is_rejected_and_closed(self):
        """Valkey / Redis 7 / anything pre-ACKED → clear gate, inline fallback."""
        client = AsyncMock()
        client.info.return_value = {"redis_version": "7.4.2"}
        settings = ClickEventsSettings(sink="stream", queue_redis_uri="redis://q/0")
        with patch(
            "app.infrastructure.queue_redis.create_redis_client",
            AsyncMock(return_value=client),
        ):
            assert await connect_queue_redis(settings) is None
        client.aclose.assert_awaited_once()

    async def test_info_failure_is_rejected(self):
        client = AsyncMock()
        client.info.side_effect = ConnectionError("blip")
        settings = ClickEventsSettings(sink="stream", queue_redis_uri="redis://q/0")
        with patch(
            "app.infrastructure.queue_redis.create_redis_client",
            AsyncMock(return_value=client),
        ):
            assert await connect_queue_redis(settings) is None
        client.aclose.assert_awaited_once()
