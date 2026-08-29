"""Smoke test: wire_services constructs the CF backend in all three
protocol slots and exposes the expected verification methods.

CF SaaS is the only backend after the LE/Caddy on-demand path was
removed; the wiring is now unconditional (no `cf_zone_id` branch). When
the operator hasn't configured CF, the service still wires but its
mutating methods short-circuit via the feature flag — see the
`CustomDomainSettings.enabled` gate.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from config import AppSettings, CustomDomainSettings
from dependencies.wiring import wire_services
from schemas.enums.domain_status import VerificationMethod
from services.cf_saas_backend import CfSaasBackend


def _wire(custom_domains: CustomDomainSettings):
    settings = AppSettings()
    settings.custom_domains = custom_domains
    app = MagicMock()
    app.state.db = {  # dict subscript so wiring db["..."] works on a MagicMock-free path
        "urlsV2": MagicMock(name="urlsV2"),
        "urls": MagicMock(name="urls"),
        "emojis": MagicMock(name="emojis"),
        "clicks": MagicMock(name="clicks"),
        "users": MagicMock(name="users"),
        "verification-tokens": MagicMock(name="tokens"),
        "api-keys": MagicMock(name="apikeys"),
        "page-layouts": MagicMock(name="page-layouts"),
        "blocked-urls": MagicMock(name="blocked-urls"),
        "app-grants": MagicMock(name="app-grants"),
        "feature_flags": MagicMock(name="feature_flags"),
        "custom_domains": MagicMock(name="custom_domains"),
        "blocked_domains": MagicMock(name="blocked_domains"),
        "reports": MagicMock(name="reports"),
        "report_submissions": MagicMock(name="report_submissions"),
        "webhook-events": MagicMock(name="webhook-events"),
        "webhook-endpoints": MagicMock(name="webhook-endpoints"),
        "webhook-deliveries": MagicMock(name="webhook-deliveries"),
        "safety_verdicts": MagicMock(name="safety_verdicts"),
        "safety_feed_domains": MagicMock(name="safety_feed_domains"),
        "scheduled_tasks": MagicMock(name="scheduled_tasks"),
    }
    app.state.http_client = MagicMock()
    app.state.geoip = MagicMock()
    app.state.email_provider = MagicMock()
    redis_client = None
    wire_services(app, settings, redis_client)
    return app


class TestCfSaasWiring:
    def test_single_cf_backend_fills_three_slots(self):
        app = _wire(
            CustomDomainSettings(
                mock_dcv=False,
                enabled=True,
                cf_zone_id="zone1",
                cf_api_token="tok",
                cf_dcv_delegation_target="abc.dcv.cloudflare.com",
            )
        )
        svc = app.state.custom_domain_service
        # Same instance in all three slots — wiring contract.
        assert isinstance(svc._edge, CfSaasBackend)
        assert isinstance(svc._registrar, CfSaasBackend)
        assert svc._edge is svc._registrar
        assert svc._verifiers[VerificationMethod.CF_DELEGATED_DCV] is svc._edge
        assert svc._verifiers[VerificationMethod.CF_HTTP_DCV] is svc._edge

    def test_only_cf_methods_registered(self):
        app = _wire(
            CustomDomainSettings(
                mock_dcv=False,
                enabled=True,
                cf_zone_id="zone1",
                cf_api_token="tok",
                cf_dcv_delegation_target="abc.dcv.cloudflare.com",
            )
        )
        svc = app.state.custom_domain_service
        # Legacy DNS methods are no longer wired by anything in the codebase.
        assert set(svc._verifiers.keys()) == {
            VerificationMethod.CF_DELEGATED_DCV,
            VerificationMethod.CF_HTTP_DCV,
        }


class TestWiringWithoutCfConfigured:
    """OSS posture: a fork that hasn't set CF env vars must still boot.
    Wiring constructs the CF backend with None credentials; the service's
    own `_require_enabled` gate (Phase 4.5) is what hides the feature."""

    def test_wires_cleanly_when_cf_unset(self):
        # No cf_zone_id / cf_api_token — should not raise at wire time.
        app = _wire(CustomDomainSettings(enabled=False, mock_dcv=False))
        svc = app.state.custom_domain_service
        assert svc is not None
        assert isinstance(svc._edge, CfSaasBackend)


class TestCfConfigValidation:
    def test_cf_zone_id_without_token_fails_at_settings_load(self):
        with pytest.raises(ValueError, match="cf_api_token"):
            CustomDomainSettings(cf_zone_id="zone1", mock_dcv=False)
