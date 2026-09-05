"""Smoke tests: AppSettings defaults and sub-config population."""

from __future__ import annotations

import os
from unittest.mock import patch

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/")

from config import (
    AppSettings,
    DatabaseSettings,
    EmailSettings,
    JWTSettings,
    LoggingSettings,
    OAuthProviderSettings,
    RedisSettings,
    SentrySettings,
)


def test_settings_loads_with_only_mongodb_uri() -> None:
    """AppSettings should load successfully with only MONGODB_URI set."""
    settings = AppSettings()
    assert settings is not None
    assert settings.db is not None
    assert settings.db.mongodb_uri is not None


def test_all_sub_configs_populated() -> None:
    """All sub-config objects should be populated after construction."""
    settings = AppSettings()
    assert isinstance(settings.db, DatabaseSettings)
    assert isinstance(settings.redis, RedisSettings)
    assert isinstance(settings.jwt, JWTSettings)
    assert isinstance(settings.oauth, OAuthProviderSettings)
    assert isinstance(settings.email, EmailSettings)
    assert isinstance(settings.logging, LoggingSettings)
    assert isinstance(settings.sentry, SentrySettings)


def test_default_cors_origins() -> None:
    """Default CORS origins should be ["*"]."""
    field = AppSettings.model_fields["cors_origins"]
    assert field.default == ["*"]


def test_default_cors_private_origins_empty() -> None:
    """Default CORS private origins should be empty (no cross-origin auth by default)."""
    field = AppSettings.model_fields["cors_private_origins"]
    assert field.default == []


def test_default_max_content_length() -> None:
    """Default max content length should be 1 MB."""
    settings = AppSettings()
    assert settings.max_content_length == 1_048_576


def test_default_env() -> None:
    """Default env should be 'development'."""
    settings = AppSettings()
    assert settings.env == "development"


def test_default_app_url() -> None:
    """The AppSettings class declares 'https://spoo.me' as the app_url default.

    At runtime the .env file may override this, so we verify the schema default
    rather than the resolved value.
    """
    field = AppSettings.model_fields["app_url"]
    assert field.default == "https://spoo.me"


def test_flask_secret_key_fallback() -> None:
    """If SECRET_KEY is empty but FLASK_SECRET_KEY is set, it should be used."""
    with patch.dict(
        os.environ,
        {"SECRET_KEY": "", "FLASK_SECRET_KEY": "flask-secret-123"},
        clear=False,
    ):
        settings = AppSettings()
        assert settings.secret_key == "flask-secret-123"


def test_default_edge_composed_errors_false() -> None:
    """EDGE_COMPOSED_ERRORS defaults off — self-hosters keep branded error pages.

    Verify the schema default rather than the resolved value so a local .env
    can't flip this test.
    """
    field = AppSettings.model_fields["edge_composed_errors"]
    assert field.default is False


def test_is_production_false_for_development() -> None:
    """is_production should return False for development env."""
    settings = AppSettings()
    assert settings.is_production is False


def test_is_production_true_for_production() -> None:
    """is_production should return True when env is 'production'."""
    with patch.dict(
        os.environ,
        {
            "ENV": "production",
            "CUSTOM_DOMAINS_MOCK_DCV": "false",
            "BILLING_PROVIDER": "none",
        },
        clear=False,
    ):
        settings = AppSettings()
        assert settings.is_production is True


def test_docs_disabled_in_fastapi() -> None:
    """Built-in docs_url should be None — Scalar docs are served via a custom /docs route."""
    from app import create_app

    app = create_app()
    assert app.docs_url is None
