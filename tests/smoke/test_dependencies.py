"""Smoke tests: dependency function imports and signatures."""

from __future__ import annotations

import inspect
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import dependencies
from app.config import AppSettings

# All dependency functions that should be importable from dependencies.py
_DEPENDENCY_NAMES = [
    "get_settings",
    "get_db",
    "get_redis",
    "get_email_provider",
    "get_geoip_service",
    "get_current_user",
    "require_auth",
    "require_verified_email",
    "get_url_service",
    "get_stats_service",
    "get_export_service",
    "get_api_key_service",
    "get_credential_service",
    "get_verification_service",
    "get_password_service",
    "get_device_auth_service",
    "get_user_repo",
    "get_oauth_service",
    "get_profile_picture_service",
    "get_contact_service",
    "get_click_service",
]


def test_all_dependencies_importable() -> None:
    """Every expected dependency function should be importable from dependencies module."""
    for name in _DEPENDENCY_NAMES:
        attr = getattr(dependencies, name, None)
        assert attr is not None, f"dependencies.{name} not found"
        assert callable(attr), f"dependencies.{name} is not callable"


def test_dependency_function_signatures() -> None:
    """Dependency functions should accept Request or use Depends parameters."""
    # Functions that take Request as first arg
    request_deps = [
        "get_settings",
        "get_db",
        "get_redis",
        "get_email_provider",
        "get_geoip_service",
        "get_contact_service",
    ]
    for name in request_deps:
        fn = getattr(dependencies, name)
        sig = inspect.signature(fn)
        params = list(sig.parameters.keys())
        assert len(params) >= 1, f"{name} should have at least one parameter"
        # First param should be 'request' or 'db' or similar
        assert params[0] in ("request", "db", "redis", "settings", "user"), (
            f"{name} first param is {params[0]}, expected a dependency-injectable param"
        )


def test_check_api_key_scope_importable() -> None:
    """check_api_key_scope should be importable and callable."""
    assert callable(dependencies.check_api_key_scope)


def test_current_user_dataclass_importable() -> None:
    """CurrentUser dataclass should be importable with expected fields."""
    from dataclasses import fields as dc_fields

    from app.dependencies import CurrentUser

    field_names = {f.name for f in dc_fields(CurrentUser)}
    assert "user_id" in field_names
    assert "email_verified" in field_names
    assert "api_key_doc" in field_names


def test_dependency_overrides_work(smoke_app: FastAPI) -> None:
    """FastAPI dependency_overrides should allow replacing get_settings."""
    custom_settings = AppSettings()
    custom_settings.app_name = "smoke-test-override"

    smoke_app.dependency_overrides[dependencies.get_settings] = lambda: custom_settings

    try:
        with TestClient(smoke_app, raise_server_exceptions=False) as client:
            # The override should be in effect — verify app still boots
            resp = client.get("/health")
            assert resp.status_code in (200, 503)
    finally:
        smoke_app.dependency_overrides.clear()
