"""Env-gated selection of the erasure mailer and PostHog eraser.

``build_erasure_mailer`` / ``build_posthog_eraser`` pick the real
integration when its settings are configured and the Noop otherwise —
AppSettings is env-only, so both branches are probed via env vars.
``build_account_erasure_service`` (the worker-side composition of the
same cascade) is probed the same way: env decides which side-effect
clients get wired, primitives are fakes.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.config import AppSettings
from app.dependencies.wiring import (
    build_account_erasure_service,
    build_erasure_mailer,
    build_posthog_eraser,
    build_r2_storage,
)
from app.infrastructure.cloudflare_kv import CloudflareKVClient
from app.infrastructure.email.zeptomail import ZeptoMailProvider
from app.infrastructure.posthog_erasure import HttpPostHogEraser
from app.infrastructure.storage.r2 import R2StorageClient
from app.services.account_erasure_service import (
    AccountErasureService,
    NoopErasureMailer,
    NoopPostHogEraser,
)
from app.services.cf_saas_backend import CfSaasBackend
from app.services.edge_cache.og_writethrough import OgEdgeWritethrough
from app.services.mock_dcv_backend import MockDcvBackend

_SIDE_EFFECT_ENV = (
    "ZEPTO_API_TOKEN",
    "POSTHOG_ERASURE_API_KEY",
    "POSTHOG_ERASURE_PROJECT_ID",
    "POSTHOG_ERASURE_HOST",
    "EDGE_CACHE_CF_ACCOUNT_ID",
    "EDGE_CACHE_CF_API_TOKEN",
    "EDGE_CACHE_KV_NAMESPACE_ID",
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET",
    "R2_PUBLIC_BASE_URL",
    "R2_ENDPOINT_URL",
    "CUSTOM_DOMAINS_MOCK_DCV",
)


def _full_r2_env(monkeypatch):
    monkeypatch.setenv("R2_ACCOUNT_ID", "acct")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("R2_BUCKET", "og-images")
    monkeypatch.setenv("R2_PUBLIC_BASE_URL", "https://og.spoo.me")


@pytest.fixture
def base_env(monkeypatch):
    monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017/")
    for var in _SIDE_EFFECT_ENV:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


class TestBuildErasureMailer:
    def test_unconfigured_token_selects_noop(self, base_env):
        mailer = build_erasure_mailer(AppSettings(), MagicMock())
        assert isinstance(mailer, NoopErasureMailer)

    def test_configured_token_selects_zepto(self, base_env):
        base_env.setenv("ZEPTO_API_TOKEN", "test-token")
        mailer = build_erasure_mailer(AppSettings(), MagicMock())
        assert isinstance(mailer, ZeptoMailProvider)

    def test_existing_provider_singleton_reused(self, base_env):
        base_env.setenv("ZEPTO_API_TOKEN", "test-token")
        provider = MagicMock(spec=ZeptoMailProvider)
        mailer = build_erasure_mailer(
            AppSettings(), MagicMock(), email_provider=provider
        )
        assert mailer is provider

    def test_unconfigured_ignores_passed_provider(self, base_env):
        # No token ⇒ Noop even when the app already built a provider.
        provider = MagicMock(spec=ZeptoMailProvider)
        mailer = build_erasure_mailer(
            AppSettings(), MagicMock(), email_provider=provider
        )
        assert isinstance(mailer, NoopErasureMailer)


class TestBuildPostHogEraser:
    def test_unconfigured_selects_noop(self, base_env):
        eraser = build_posthog_eraser(AppSettings(), MagicMock())
        assert isinstance(eraser, NoopPostHogEraser)

    def test_key_without_project_id_selects_noop(self, base_env):
        base_env.setenv("POSTHOG_ERASURE_API_KEY", "phx_secret")
        eraser = build_posthog_eraser(AppSettings(), MagicMock())
        assert isinstance(eraser, NoopPostHogEraser)

    def test_configured_selects_http_eraser(self, base_env):
        base_env.setenv("POSTHOG_ERASURE_API_KEY", "phx_secret")
        base_env.setenv("POSTHOG_ERASURE_PROJECT_ID", "12345")
        eraser = build_posthog_eraser(AppSettings(), MagicMock())
        assert isinstance(eraser, HttpPostHogEraser)
        assert eraser._project_id == "12345"
        assert eraser._host == "https://eu.posthog.com"

    def test_host_override(self, base_env):
        base_env.setenv("POSTHOG_ERASURE_API_KEY", "phx_secret")
        base_env.setenv("POSTHOG_ERASURE_PROJECT_ID", "12345")
        base_env.setenv("POSTHOG_ERASURE_HOST", "https://us.posthog.com/")
        eraser = build_posthog_eraser(AppSettings(), MagicMock())
        assert isinstance(eraser, HttpPostHogEraser)
        assert eraser._host == "https://us.posthog.com"


class TestBuildR2Storage:
    """The shared R2 construction gate for the app and the worker."""

    def test_unconfigured_yields_none(self, base_env):
        assert build_r2_storage(AppSettings(), MagicMock()) is None

    def test_configured_yields_client(self, base_env):
        _full_r2_env(base_env)
        assert isinstance(build_r2_storage(AppSettings(), MagicMock()), R2StorageClient)

    def test_loopback_http_endpoint_yields_client(self, base_env):
        _full_r2_env(base_env)
        base_env.setenv("R2_ENDPOINT_URL", "http://localhost:9000")
        assert isinstance(build_r2_storage(AppSettings(), MagicMock()), R2StorageClient)

    def test_insecure_endpoint_degrades_to_none_with_warning(self, base_env):
        """A compose self-host pointing at http://minio:9000 must boot with
        uploads disabled — the same degraded state as unconfigured R2 —
        instead of crashing on the client's https guard."""
        _full_r2_env(base_env)
        base_env.setenv("R2_ENDPOINT_URL", "http://minio:9000")
        with patch("app.dependencies.wiring.log", new=MagicMock()) as mock_log:
            assert build_r2_storage(AppSettings(), MagicMock()) is None
        event = mock_log.warning.call_args.args[0]
        assert event == "r2_storage_disabled_insecure_endpoint"
        assert "https" in mock_log.warning.call_args.kwargs["reason"]


class TestBuildAccountErasureService:
    """Worker-side composition root: same env gates as the app wiring."""

    def _build(self):
        # Mock db mapping, inert http client, redis None (cache
        # invalidation degrades to no-ops, as in workers without Redis).
        return build_account_erasure_service(
            MagicMock(), AppSettings(), MagicMock(), None
        )

    def test_minimal_env_wires_disabled_side_effects(self, base_env):
        service = self._build()
        assert isinstance(service, AccountErasureService)
        # Edge cache + R2 unconfigured ⇒ no KV purge, no og write-through,
        # no R2 sweep.
        assert service._r2_storage is None
        assert service._url_service._edge_kv is None
        assert service._url_service._og_writethrough is None
        assert service._url_service._r2_storage is None
        # Unconfigured mail/PostHog degrade to Noops, as in the app.
        assert isinstance(service._mailer, NoopErasureMailer)
        assert isinstance(service._posthog, NoopPostHogEraser)

    def test_insecure_r2_endpoint_degrades_like_unconfigured(self, base_env):
        _full_r2_env(base_env)
        base_env.setenv("R2_ENDPOINT_URL", "http://minio:9000")
        service = self._build()
        assert service._r2_storage is None
        assert service._url_service._r2_storage is None

    def test_default_dcv_selects_cf_saas_backend(self, base_env):
        service = self._build()
        assert isinstance(service._domain_service._edge, CfSaasBackend)

    def test_mock_dcv_selects_mock_backend(self, base_env):
        base_env.setenv("CUSTOM_DOMAINS_MOCK_DCV", "true")
        service = self._build()
        assert isinstance(service._domain_service._edge, MockDcvBackend)

    def test_configured_edge_and_r2_wire_real_clients(self, base_env):
        base_env.setenv("EDGE_CACHE_CF_ACCOUNT_ID", "acct")
        base_env.setenv("EDGE_CACHE_CF_API_TOKEN", "kv-token")
        base_env.setenv("EDGE_CACHE_KV_NAMESPACE_ID", "ns")
        _full_r2_env(base_env)
        service = self._build()
        url_service = service._url_service
        assert isinstance(url_service._edge_kv, CloudflareKVClient)
        assert isinstance(url_service._og_writethrough, OgEdgeWritethrough)
        assert isinstance(url_service._r2_storage, R2StorageClient)
        # The erasure R2 sweep and the URL service delete path must share
        # ONE client — a split here would orphan uploaded og:images.
        assert service._r2_storage is url_service._r2_storage
