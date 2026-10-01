"""Tests for ClickEventsSettings defaults, env parsing, and sink wiring."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.config import AppSettings, ClickEventsSettings


class TestClickEventsSettings:
    def test_defaults_are_inline_and_safe(self):
        s = ClickEventsSettings()
        assert s.sink == "inline"
        assert s.queue_redis_uri == ""
        assert s.stream == "events:clicks"
        assert s.dlq_stream == "events:clicks:dlq"
        assert s.maxlen == 10_000
        assert s.max_deliveries == 5
        assert s.claim_idle_ms == 60_000
        assert s.hotness_enabled is False
        assert s.hot_threshold == 50
        assert s.hot_window_seconds == 60
        assert s.worker_groups == ["stats", "hotness"]

    def test_env_prefix(self, monkeypatch):
        monkeypatch.setenv("CLICK_EVENTS_SINK", "stream")
        monkeypatch.setenv("CLICK_EVENTS_QUEUE_REDIS_URI", "redis://q:6379/0")
        monkeypatch.setenv("CLICK_EVENTS_HOT_THRESHOLD", "10")
        s = ClickEventsSettings()
        assert s.sink == "stream"
        assert s.queue_redis_uri == "redis://q:6379/0"
        assert s.hot_threshold == 10

    def test_unprefixed_env_vars_are_ignored(self, monkeypatch):
        """Generic names set elsewhere in the deploy env must not leak in."""
        monkeypatch.setenv("SINK", "stream")
        monkeypatch.setenv("STREAM", "other")
        s = ClickEventsSettings()
        assert s.sink == "inline"
        assert s.stream == "events:clicks"

    def test_invalid_sink_rejected(self, monkeypatch):
        monkeypatch.setenv("CLICK_EVENTS_SINK", "kafka")
        with pytest.raises(PydanticValidationError):
            ClickEventsSettings()

    def test_zero_or_negative_tunables_rejected(self):
        with pytest.raises(PydanticValidationError):
            ClickEventsSettings(batch_size=0)
        with pytest.raises(PydanticValidationError):
            ClickEventsSettings(max_deliveries=0)
        with pytest.raises(PydanticValidationError):
            ClickEventsSettings(hot_threshold=1)  # 1 would fire on every click
        with pytest.raises(PydanticValidationError):
            ClickEventsSettings(hot_window_seconds=0)

    def test_worker_groups_parse_from_env(self, monkeypatch):
        monkeypatch.setenv("CLICK_EVENTS_WORKER_GROUPS", '["stats"]')
        s = ClickEventsSettings()
        assert s.worker_groups == ["stats"]

    def test_unknown_worker_group_rejected(self):
        with pytest.raises(PydanticValidationError):
            ClickEventsSettings(worker_groups=["stats", "nonsense"])

    def test_app_settings_composes_click_events(self):
        s = AppSettings()
        assert s.click_events is not None
        assert s.click_events.sink in ("inline", "stream")
