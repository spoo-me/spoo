"""Smoke tests: application startup and basic health."""

from __future__ import annotations

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/")

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_app_boots_without_error(smoke_app: FastAPI) -> None:
    """TestClient context manager should not raise during startup/shutdown."""
    with TestClient(smoke_app, raise_server_exceptions=False):
        pass


def test_health_endpoint_responds(smoke_client: TestClient) -> None:
    """Liveness probe — always 200, no dependency checks."""
    resp = smoke_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_app_title_and_version(smoke_app: FastAPI) -> None:
    """App metadata should match expected values."""
    assert smoke_app.title == "spoo.me"
    assert smoke_app.version == "1.0.0"


def test_app_state_attributes_after_startup(smoke_app: FastAPI) -> None:
    """After lifespan startup, app.state should have all required attributes."""
    with TestClient(smoke_app, raise_server_exceptions=False):
        assert hasattr(smoke_app.state, "settings")
        assert hasattr(smoke_app.state, "db")
        assert hasattr(smoke_app.state, "redis")
        assert hasattr(smoke_app.state, "email_provider")
        assert hasattr(smoke_app.state, "http_client")
        assert hasattr(smoke_app.state, "oauth_providers")
        assert hasattr(smoke_app.state, "geoip")
        assert hasattr(smoke_app.state, "limiter")
