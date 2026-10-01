"""Unit tests for AppSettings and sub-configs."""

import pytest
from pydantic import ValidationError as PydanticValidationError

from config import (
    AppSettings,
    CustomDomainSettings,
    DatabaseSettings,
    JWTSettings,
    RedisSettings,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def with_mongo(monkeypatch):
    """Set the required MONGODB_URI so AppSettings can be instantiated."""
    monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017/")
    return monkeypatch


# ---------------------------------------------------------------------------
# DatabaseSettings
# ---------------------------------------------------------------------------


class TestDatabaseSettings:
    def test_loads_mongodb_uri(self, monkeypatch):
        monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017/")
        assert DatabaseSettings().mongodb_uri == "mongodb://localhost:27017/"

    def test_default_db_name(self, monkeypatch):
        monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017/")
        assert DatabaseSettings().db_name == "url-shortener"

    def test_missing_mongodb_uri_raises(self, monkeypatch):
        monkeypatch.delenv("MONGODB_URI", raising=False)
        with pytest.raises((PydanticValidationError, Exception)):
            DatabaseSettings()


# ---------------------------------------------------------------------------
# RedisSettings
# ---------------------------------------------------------------------------


class TestRedisSettings:
    def test_redis_uri_optional(self, monkeypatch):
        monkeypatch.delenv("REDIS_URI", raising=False)
        assert RedisSettings().redis_uri is None

    def test_redis_uri_loaded(self, monkeypatch):
        monkeypatch.setenv("REDIS_URI", "redis://localhost:6379")
        assert RedisSettings().redis_uri == "redis://localhost:6379"

    def test_default_ttl(self, monkeypatch):
        monkeypatch.delenv("REDIS_TTL_SECONDS", raising=False)
        assert RedisSettings().redis_ttl_seconds == 3600


# ---------------------------------------------------------------------------
# JWTSettings
# ---------------------------------------------------------------------------


class TestJWTSettings:
    def test_defaults(self, monkeypatch):
        for var in (
            "JWT_ISSUER",
            "JWT_AUDIENCE",
            "ACCESS_TOKEN_TTL_SECONDS",
            "REFRESH_TOKEN_TTL_SECONDS",
            "COOKIE_SECURE",
            "JWT_PRIVATE_KEY",
            "JWT_PUBLIC_KEY",
            "JWT_SECRET",
        ):
            monkeypatch.delenv(var, raising=False)
        s = JWTSettings()
        assert s.jwt_issuer == "spoo.me"
        assert s.jwt_audience == "spoo.me.api"
        assert s.access_token_ttl_seconds == 900
        assert s.refresh_token_ttl_seconds == 2592000


@pytest.mark.parametrize(
    "private_key, public_key, expected",
    [
        ("private", "public", True),
        (None, None, False),
    ],
    ids=["keys_present", "keys_absent"],
)
def test_jwt_use_rs256(monkeypatch, private_key, public_key, expected):
    if private_key:
        monkeypatch.setenv("JWT_PRIVATE_KEY", private_key)
        monkeypatch.setenv("JWT_PUBLIC_KEY", public_key)
    else:
        monkeypatch.delenv("JWT_PRIVATE_KEY", raising=False)
        monkeypatch.delenv("JWT_PUBLIC_KEY", raising=False)
    assert JWTSettings().use_rs256 is expected


# ---------------------------------------------------------------------------
# AppSettings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env, expected",
    [("production", True), ("development", False)],
    ids=["production", "development"],
)
def test_is_production(with_mongo, env, expected):
    with_mongo.setenv("ENV", env)
    with_mongo.setenv("BILLING_PROVIDER", "none")
    assert AppSettings().is_production is expected


@pytest.mark.parametrize(
    "env, should_boot",
    [("production", False), ("development", True)],
    ids=["production_refuses", "development_allows"],
)
def test_mock_dcv_refused_in_production(with_mongo, env, should_boot):
    # The DCV mock verifies unconditionally; a mis-set env var in prod
    # would let anyone claim any domain. Startup must refuse, not warn.
    with_mongo.setenv("ENV", env)
    with_mongo.setenv("CUSTOM_DOMAINS_MOCK_DCV", "true")
    if should_boot:
        assert AppSettings().custom_domains.mock_dcv is True
    else:
        with pytest.raises(PydanticValidationError, match="MOCK_DCV"):
            AppSettings()


@pytest.mark.parametrize(
    "env, should_boot",
    [("production", False), ("development", True)],
    ids=["production_refuses", "development_allows"],
)
def test_zero_grace_days_refused_in_production(with_mongo, env, should_boot):
    # Zero grace purges on the next sweep — fine for integration smoke,
    # but in production it would void the whole restore window.
    with_mongo.setenv("ENV", env)
    with_mongo.setenv("ACCOUNT_DELETION_GRACE_DAYS", "0")
    if should_boot:
        assert AppSettings().account_deletion_grace_days == 0
    else:
        with pytest.raises(PydanticValidationError, match="GRACE_DAYS"):
            AppSettings()


def test_positive_grace_days_boots_in_production(with_mongo):
    with_mongo.setenv("ENV", "production")
    with_mongo.setenv("BILLING_PROVIDER", "none")
    with_mongo.setenv("ACCOUNT_DELETION_GRACE_DAYS", "7")
    assert AppSettings().account_deletion_grace_days == 7


@pytest.mark.parametrize(
    "env, should_boot",
    [("production", False), ("development", True)],
    ids=["production_refuses", "development_allows"],
)
def test_unset_billing_provider_refused_in_production(with_mongo, env, should_boot):
    # The default is self-host, which hands every account every feature.
    with_mongo.setenv("ENV", env)
    with_mongo.delenv("BILLING_PROVIDER", raising=False)
    if should_boot:
        assert AppSettings().billing.selfhost is True
    else:
        with pytest.raises(PydanticValidationError, match="BILLING_PROVIDER"):
            AppSettings()


def test_explicit_billing_provider_boots_in_production(with_mongo):
    with_mongo.setenv("ENV", "production")
    with_mongo.setenv("BILLING_PROVIDER", "none")
    assert AppSettings().billing.selfhost is True


@pytest.mark.parametrize(
    "secret_key, flask_secret_key, expected",
    [
        (None, "flask-secret", "flask-secret"),  # falls back to FLASK_SECRET_KEY
        ("new-secret", "old-secret", "new-secret"),  # SECRET_KEY takes precedence
    ],
    ids=["flask_fallback", "secret_key_wins"],
)
def test_secret_key_resolution(with_mongo, secret_key, flask_secret_key, expected):
    if secret_key:
        with_mongo.setenv("SECRET_KEY", secret_key)
    else:
        with_mongo.delenv("SECRET_KEY", raising=False)
    with_mongo.setenv("FLASK_SECRET_KEY", flask_secret_key)
    assert AppSettings().secret_key == expected


class TestAppSettings:
    def test_sub_configs_populated(self, with_mongo):
        s = AppSettings()
        for attr in ("db", "redis", "jwt", "oauth", "email", "logging", "sentry"):
            assert getattr(s, attr) is not None, f"sub-config '{attr}' is None"

    def test_cors_origins_default(self, with_mongo):
        assert AppSettings().cors_origins == ["*"]


class TestSystemDefaultDomain:
    """AppSettings.system_default_domain — fail-hard derivation from app_url."""

    def test_extracts_fqdn_from_app_url(self, with_mongo):
        with_mongo.setenv("APP_URL", "https://spoo.me")
        assert AppSettings().system_default_domain == "spoo.me"

    def test_lowercases(self, with_mongo):
        with_mongo.setenv("APP_URL", "https://SPOO.ME")
        assert AppSettings().system_default_domain == "spoo.me"

    def test_strips_port(self, with_mongo):
        with_mongo.setenv("APP_URL", "http://localhost:8000")
        assert AppSettings().system_default_domain == "localhost"

    def test_self_hoster_domain(self, with_mongo):
        with_mongo.setenv("APP_URL", "https://my.shortener.dev")
        assert AppSettings().system_default_domain == "my.shortener.dev"

    def test_strips_trailing_dot(self, with_mongo):
        # Fully qualified DNS notation includes a trailing dot for the root.
        # Strip it so the canonical form matches everywhere.
        with_mongo.setenv("APP_URL", "https://spoo.me./")
        assert AppSettings().system_default_domain == "spoo.me"

    def test_raises_on_missing_scheme(self, with_mongo):
        # Bare domain, no scheme → urlparse can't extract hostname.
        with_mongo.setenv("APP_URL", "spoo.me")
        with pytest.raises(ValueError, match="APP_URL is missing or invalid"):
            _ = AppSettings().system_default_domain

    def test_raises_on_empty(self, with_mongo):
        with_mongo.setenv("APP_URL", "")
        with pytest.raises(ValueError, match="APP_URL is missing or invalid"):
            _ = AppSettings().system_default_domain

    def test_raises_on_garbage(self, with_mongo):
        with_mongo.setenv("APP_URL", "not a url")
        with pytest.raises(ValueError, match="APP_URL is missing or invalid"):
            _ = AppSettings().system_default_domain

    def test_blocked_self_domains_derives_from_default(self, with_mongo):
        with_mongo.setenv("APP_URL", "https://spoo.me")
        assert AppSettings().blocked_self_domains == ("spoo.me",)


class TestCustomDomainSettings:
    def test_default_is_disabled(self, monkeypatch):
        # Sanity: default-deny so any deploy that forgets to set the
        # explicit env var keeps the feature off.
        # Clear any inherited env var so the test is hermetic.
        monkeypatch.delenv("CUSTOM_DOMAINS_ENABLED", raising=False)
        monkeypatch.delenv("ENABLED", raising=False)
        assert CustomDomainSettings().enabled is False

    def test_generic_enabled_env_var_does_not_leak_in(self, monkeypatch):
        # Regression: without env_prefix, a generic ENABLED=true set by an
        # unrelated CI/deploy script would have flipped the master switch.
        monkeypatch.setenv("ENABLED", "true")
        monkeypatch.delenv("CUSTOM_DOMAINS_ENABLED", raising=False)
        assert CustomDomainSettings().enabled is False

    def test_prefixed_env_var_is_honoured(self, monkeypatch):
        monkeypatch.setenv("CUSTOM_DOMAINS_ENABLED", "true")
        assert CustomDomainSettings().enabled is True

    def test_prefixed_max_per_user_overrides_default(self, monkeypatch):
        monkeypatch.setenv("CUSTOM_DOMAINS_MAX_PER_USER", "10")
        assert CustomDomainSettings().max_per_user == 10


class TestWebhookSettingsGuard:
    def test_enabled_webhooks_require_secret_key(self, monkeypatch):
        """An empty master secret would derive a predictable encryption
        key for signing secrets — startup must refuse, not warn."""
        import pytest as _pytest

        from config import AppSettings

        monkeypatch.setenv("WEBHOOKS_ENABLED", "true")
        monkeypatch.delenv("SECRET_KEY", raising=False)
        monkeypatch.delenv("FLASK_SECRET_KEY", raising=False)
        with _pytest.raises(Exception, match="SECRET_KEY"):
            AppSettings(secret_key="", flask_secret_key="")

    def test_enabled_webhooks_accept_configured_secret(self, monkeypatch):
        from config import AppSettings

        monkeypatch.setenv("WEBHOOKS_ENABLED", "true")
        settings = AppSettings(secret_key="a-real-secret")
        assert settings.webhooks.enabled is True


class TestAccountDeletionSettings:
    def test_defaults(self, with_mongo):
        settings = AppSettings()
        assert settings.account_deletion_grace_days == 7
        assert settings.account_erasure_batch_limit == 25

    def test_env_override(self, with_mongo):
        with_mongo.setenv("ACCOUNT_DELETION_GRACE_DAYS", "30")
        with_mongo.setenv("ACCOUNT_ERASURE_BATCH_LIMIT", "100")
        settings = AppSettings()
        assert settings.account_deletion_grace_days == 30
        assert settings.account_erasure_batch_limit == 100

    def test_grace_zero_allowed_for_immediate_purge(self, with_mongo):
        with_mongo.setenv("ACCOUNT_DELETION_GRACE_DAYS", "0")
        assert AppSettings().account_deletion_grace_days == 0

    def test_negative_grace_days_rejected(self, with_mongo):
        with_mongo.setenv("ACCOUNT_DELETION_GRACE_DAYS", "-1")
        with pytest.raises(PydanticValidationError):
            AppSettings()

    def test_zero_batch_limit_rejected(self, with_mongo):
        with_mongo.setenv("ACCOUNT_ERASURE_BATCH_LIMIT", "0")
        with pytest.raises(PydanticValidationError):
            AppSettings()


class TestPostHogErasureSettings:
    def test_defaults_disabled(self, with_mongo):
        settings = AppSettings()
        assert settings.posthog_erasure.api_key == ""
        assert settings.posthog_erasure.project_id == ""
        assert settings.posthog_erasure.host == "https://eu.posthog.com"
        assert settings.posthog_erasure.enabled is False

    def test_enabled_requires_key_and_project(self, with_mongo):
        with_mongo.setenv("POSTHOG_ERASURE_API_KEY", "phx_secret")
        assert AppSettings().posthog_erasure.enabled is False
        with_mongo.setenv("POSTHOG_ERASURE_PROJECT_ID", "12345")
        assert AppSettings().posthog_erasure.enabled is True

    def test_env_prefix(self, with_mongo):
        with_mongo.setenv("POSTHOG_ERASURE_API_KEY", "phx_secret")
        with_mongo.setenv("POSTHOG_ERASURE_PROJECT_ID", "12345")
        with_mongo.setenv("POSTHOG_ERASURE_HOST", "https://us.posthog.com")
        settings = AppSettings()
        assert settings.posthog_erasure.api_key == "phx_secret"
        assert settings.posthog_erasure.project_id == "12345"
        assert settings.posthog_erasure.host == "https://us.posthog.com"

    def test_plain_http_host_is_a_boot_error(self, with_mongo):
        """The key is person-deletion-scoped — a typo'd http host must fail
        at boot, never send it over plaintext mid-sweep."""
        with_mongo.setenv("POSTHOG_ERASURE_HOST", "http://eu.posthog.com")
        with pytest.raises(PydanticValidationError, match="https"):
            AppSettings()
