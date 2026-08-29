"""Smoke test: wire_services resolves the click sink from settings.

Contract: inline is the default and the fallback for every misconfigured
state; the stream sink only wires when stream mode is requested AND the
queue Redis actually connected. The stream sink always carries an inline
fallback so XADD failures degrade instead of erroring.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from config import AppSettings, ClickEventsSettings
from dependencies.wiring import wire_services
from services.click.sinks import InlineSink, RedisStreamSink

_COLLECTIONS = (
    "urlsV2",
    "urls",
    "emojis",
    "clicks",
    "users",
    "verification-tokens",
    "api-keys",
    "page-layouts",
    "blocked-urls",
    "app-grants",
    "feature_flags",
    "custom_domains",
    "blocked_domains",
    "reports",
    "report_submissions",
    "webhook-events",
    "webhook-endpoints",
    "webhook-deliveries",
    "safety_verdicts",
    "safety_feed_domains",
    "scheduled_tasks",
)


def _wire(click_events: ClickEventsSettings, queue_redis):
    settings = AppSettings()
    settings.click_events = click_events
    app = MagicMock()
    app.state.db = {name: MagicMock(name=name) for name in _COLLECTIONS}
    app.state.http_client = MagicMock()
    app.state.geoip = MagicMock()
    app.state.email_provider = MagicMock()
    app.state.queue_redis = queue_redis
    wire_services(app, settings, redis_client=None)
    return app


class TestClickSinkWiring:
    def test_default_wires_inline_sink(self):
        app = _wire(ClickEventsSettings(), queue_redis=None)
        assert isinstance(app.state.click_sink, InlineSink)

    def test_stream_mode_with_queue_redis_wires_stream_sink(self):
        app = _wire(
            ClickEventsSettings(sink="stream", queue_redis_uri="redis://q:6379/0"),
            queue_redis=AsyncMock(),
        )
        assert isinstance(app.state.click_sink, RedisStreamSink)

    def test_stream_mode_without_queue_redis_falls_back_inline(self):
        """Requested stream mode but the queue Redis never connected."""
        app = _wire(
            ClickEventsSettings(sink="stream", queue_redis_uri="redis://q:6379/0"),
            queue_redis=None,
        )
        assert isinstance(app.state.click_sink, InlineSink)

    def test_inline_mode_ignores_available_queue_redis(self):
        """Explicit inline stays inline even if a queue Redis happens to exist."""
        app = _wire(ClickEventsSettings(sink="inline"), queue_redis=AsyncMock())
        assert isinstance(app.state.click_sink, InlineSink)

    def test_stream_sink_fallback_is_inline_over_same_click_service(self):
        app = _wire(
            ClickEventsSettings(sink="stream", queue_redis_uri="redis://q:6379/0"),
            queue_redis=AsyncMock(),
        )
        sink = app.state.click_sink
        assert isinstance(sink._fallback, InlineSink)
        assert sink._fallback._click_service is app.state.click_service
