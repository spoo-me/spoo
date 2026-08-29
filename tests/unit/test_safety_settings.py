"""Unit tests for SafetySettings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from config import AppSettings, SafetySettings


class TestSafetySettings:
    def test_defaults_are_off_and_safe(self):
        s = SafetySettings()
        assert s.enabled is False
        assert s.stream == "events:safety"
        assert s.dlq_stream == "events:safety:dlq"
        assert s.reverdict_ttl_hours == 24

    def test_env_prefix(self, monkeypatch):
        monkeypatch.setenv("SAFETY_ENABLED", "true")
        monkeypatch.setenv("SAFETY_REVERDICT_TTL_HOURS", "6")
        s = SafetySettings()
        assert s.enabled is True
        assert s.reverdict_ttl_hours == 6

    def test_unprefixed_env_vars_are_ignored(self, monkeypatch):
        monkeypatch.setenv("ENABLED", "true")
        assert SafetySettings().enabled is False

    def test_invalid_tunables_rejected(self):
        with pytest.raises(PydanticValidationError):
            SafetySettings(reverdict_ttl_hours=0)
        with pytest.raises(PydanticValidationError):
            SafetySettings(maxlen=10)

    def test_populated_on_app_settings(self):
        settings = AppSettings()
        assert isinstance(settings.safety, SafetySettings)


class TestFeedSettings:
    def test_feed_defaults(self):
        s = SafetySettings()
        assert s.fishfish_enabled is True
        assert s.fishfish_api_url == "https://api.fishfish.gg/v1/domains"
        assert s.web_risk_api_key == ""
        assert s.web_risk_enabled is False

    def test_web_risk_enabled_by_key(self, monkeypatch):
        monkeypatch.setenv("SAFETY_WEB_RISK_API_KEY", "AIzaTest")
        assert SafetySettings().web_risk_enabled is True


class TestL1Settings:
    def test_l1_defaults(self):
        s = SafetySettings()
        assert s.l1_enabled is True
        assert s.l1_burst_window_seconds == 600
        assert s.l1_domain_burst_threshold == 25
        assert s.l1_domain_daily_threshold == 150

    def test_l1_thresholds_reject_degenerate_values(self):
        with pytest.raises(PydanticValidationError):
            SafetySettings(l1_domain_burst_threshold=1)
        with pytest.raises(PydanticValidationError):
            SafetySettings(l1_burst_window_seconds=10)


class TestSweepSettings:
    def test_sweep_defaults(self):
        s = SafetySettings()
        assert s.sweep_recent_enabled is True
        assert s.sweep_recent_window_hours == 48
        assert s.sweep_max_enqueues == 1000

    def test_sweep_tunables_validated(self):
        with pytest.raises(PydanticValidationError):
            SafetySettings(sweep_recent_window_hours=0)
        with pytest.raises(PydanticValidationError):
            SafetySettings(sweep_max_enqueues=1)
