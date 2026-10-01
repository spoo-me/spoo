"""
Security integration tests — JWT validation, CSRF, and XSS prevention.

Verifies that the auth dependency correctly rejects expired, mis-issued,
wrong-audience, malformed, and wrong-type tokens.  Also covers CSRF
enforcement on device consent and basic XSS escaping.
"""

from __future__ import annotations

import os
import time

import jwt as pyjwt
from bson import ObjectId
from fastapi.testclient import TestClient

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/")

from app.config import AppSettings
from app.routes.auth import router as auth_router
from tests.conftest import build_test_app

# ── Helpers ──────────────────────────────────────────────────────────────────

_settings = AppSettings()
_jwt_cfg = _settings.jwt

_USER_ID = str(ObjectId())


def _make_token(extra_claims: dict | None = None, **overrides) -> str:
    """Build a JWT signed with the real secret but with customisable claims."""
    now = int(time.time())
    payload = {
        "sub": _USER_ID,
        "iss": _jwt_cfg.jwt_issuer,
        "aud": _jwt_cfg.jwt_audience,
        "iat": now,
        "exp": now + 900,
        "email_verified": True,
        "type": "access",
        "amr": ["pwd"],
    }
    if extra_claims:
        payload.update(extra_claims)
    payload.update(overrides)
    return pyjwt.encode(payload, _jwt_cfg.jwt_secret, algorithm="HS256")


# ── JWT Validation Tests ─────────────────────────────────────────────────────


class TestExpiredJWT:
    """An expired token must be rejected with 401."""

    def test_expired_jwt_returns_401(self):
        token = _make_token(exp=int(time.time()) - 60)
        app = build_test_app(auth_router)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401


class TestWrongIssuer:
    """A token with an unexpected issuer must be rejected."""

    def test_wrong_issuer_returns_401(self):
        token = _make_token(iss="evil.example.com")
        app = build_test_app(auth_router)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401


class TestWrongAudience:
    """A token with a mismatched audience must be rejected."""

    def test_wrong_audience_returns_401(self):
        token = _make_token(aud="wrong.audience")
        app = build_test_app(auth_router)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401


class TestMalformedJWT:
    """A garbage bearer value must not crash the server — just 401."""

    def test_malformed_jwt_returns_401(self):
        app = build_test_app(auth_router)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get(
                "/auth/me", headers={"Authorization": "Bearer not.a.valid.jwt"}
            )
        assert resp.status_code == 401


class TestRefreshTokenAsAccessToken:
    """A refresh token must not be accepted as an access token.

    ``get_current_user`` checks ``claims.get("type") == "refresh"`` and
    returns None, which cascades to a 401 via ``require_auth``.
    """

    def test_refresh_token_rejected_as_access_token(self):
        token = _make_token(type="refresh")
        app = build_test_app(auth_router)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401


# ── CSRF Tests ───────────────────────────────────────────────────────────────


class TestDeviceConsentCSRF:
    """The device consent POST endpoint must enforce CSRF via cookie."""

    def _consent_app(self):
        """Build an app whose JwtUser dependency is satisfied."""
        from app.dependencies import require_jwt
        from app.dependencies.auth import CurrentUser

        fake_user = CurrentUser(user_id=ObjectId(_USER_ID), email_verified=True)
        return build_test_app(
            auth_router,
            overrides={require_jwt: lambda: fake_user},
        )

    def test_missing_csrf_cookie_returns_403(self):
        app = self._consent_app()
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/auth/device/consent",
                data={
                    "app_id": "spoo-snap",
                    "state": "abc",
                    "csrf_token": "some-token",
                    "redirect_uri": "",
                },
            )
        assert resp.status_code == 403

    def test_mismatched_csrf_returns_403(self):
        app = self._consent_app()
        with TestClient(app, raise_server_exceptions=False) as client:
            client.cookies.set("_consent_csrf", "cookie-value")
            resp = client.post(
                "/auth/device/consent",
                data={
                    "app_id": "spoo-snap",
                    "state": "abc",
                    "csrf_token": "different-value",
                    "redirect_uri": "",
                },
            )
        assert resp.status_code == 403


# ── XSS Prevention ──────────────────────────────────────────────────────────


class TestXSSInAlias:
    """Script tags in user input must be escaped in HTML responses."""

    def test_script_tag_in_alias_is_escaped(self):
        app = build_test_app(auth_router)
        xss_alias = "<script>alert(1)</script>"
        with TestClient(app, raise_server_exceptions=False) as client:
            # Attempt to access a short URL with a script tag alias.
            # The server should either 404 or render an HTML page with the
            # alias escaped — the raw script tag must never appear unescaped.
            resp = client.get(f"/{xss_alias}")
        body = resp.text
        assert "<script>alert(1)</script>" not in body
