"""Unit tests for wildcard redirect URI matching."""

from __future__ import annotations

from schemas.models.app import AppEntry
from services.auth.device import DeviceAuthService


def _app(redirect_uris: list[str]) -> AppEntry:
    return AppEntry(name="test", description="test app", redirect_uris=redirect_uris)


class TestValidateRedirectUri:
    """Tests for DeviceAuthService.validate_redirect_uri."""

    def setup_method(self):
        self.svc = DeviceAuthService.__new__(DeviceAuthService)

    def test_empty_uri_allowed(self):
        app = _app(["https://example.com/callback"])
        assert self.svc.validate_redirect_uri("", app) is True

    def test_exact_match(self):
        app = _app(["https://example.com/callback"])
        assert (
            self.svc.validate_redirect_uri("https://example.com/callback", app) is True
        )

    def test_exact_no_match(self):
        app = _app(["https://example.com/callback"])
        assert self.svc.validate_redirect_uri("https://evil.com/callback", app) is False

    def test_wildcard_query_match(self):
        app = _app(["https://raycast.com/redirect?*"])
        assert (
            self.svc.validate_redirect_uri(
                "https://raycast.com/redirect?packageName=spoo&state=abc", app
            )
            is True
        )

    def test_wildcard_prefix_only(self):
        app = _app(["https://raycast.com/redirect?*"])
        assert (
            self.svc.validate_redirect_uri("https://raycast.com/redirect?", app) is True
        )

    def test_wildcard_rejects_different_path(self):
        app = _app(["https://raycast.com/redirect?*"])
        assert (
            self.svc.validate_redirect_uri("https://raycast.com/redirected", app)
            is False
        )

    def test_wildcard_rejects_different_host(self):
        app = _app(["https://raycast.com/redirect?*"])
        assert (
            self.svc.validate_redirect_uri("https://evil.com/redirect?foo=bar", app)
            is False
        )

    def test_no_uris_rejects(self):
        app = _app([])
        assert self.svc.validate_redirect_uri("https://example.com", app) is False

    def test_multiple_uris_mixed(self):
        app = _app(["https://a.com/cb", "https://b.com/redirect?*"])
        assert self.svc.validate_redirect_uri("https://a.com/cb", app) is True
        assert self.svc.validate_redirect_uri("https://b.com/redirect?x=1", app) is True
        assert self.svc.validate_redirect_uri("https://c.com/cb", app) is False

    def test_bare_star_matches_everything(self):
        """A bare '*' entry matches any URI — intentional if configured."""
        app = _app(["*"])
        assert (
            self.svc.validate_redirect_uri("https://anything.com/whatever", app) is True
        )
