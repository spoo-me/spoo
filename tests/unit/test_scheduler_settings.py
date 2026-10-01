"""Unit tests for SchedulerSettings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.config import AppSettings, SchedulerSettings


class TestSchedulerSettings:
    def test_defaults(self):
        s = SchedulerSettings()
        assert s.enabled is True
        assert s.runtime == "auto"
        assert s.poll_seconds == 5.0
        assert s.lease_seconds == 600

    def test_env_prefix(self, monkeypatch):
        monkeypatch.setenv("SCHEDULER_RUNTIME", "worker")
        monkeypatch.setenv("SCHEDULER_POLL_SECONDS", "2.5")
        s = SchedulerSettings()
        assert s.runtime == "worker"
        assert s.poll_seconds == 2.5

    def test_unprefixed_env_vars_are_ignored(self, monkeypatch):
        monkeypatch.setenv("RUNTIME", "off")
        monkeypatch.setenv("ENABLED", "false")
        s = SchedulerSettings()
        assert s.runtime == "auto"
        assert s.enabled is True

    def test_invalid_runtime_rejected(self, monkeypatch):
        monkeypatch.setenv("SCHEDULER_RUNTIME", "sometimes")
        with pytest.raises(PydanticValidationError):
            SchedulerSettings()

    def test_zero_or_negative_tunables_rejected(self):
        with pytest.raises(PydanticValidationError):
            SchedulerSettings(poll_seconds=0)
        with pytest.raises(PydanticValidationError):
            SchedulerSettings(lease_seconds=5)

    def test_populated_on_app_settings(self):
        settings = AppSettings()
        assert isinstance(settings.scheduler, SchedulerSettings)
