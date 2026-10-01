"""
Integration tests for scoped device-app tokens (scp claim) on real routes.

Uses real JWTs signed with the test secret so the full get_current_user →
scope-check dependency chain runs; only services/repos are mocked.
"""

from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock

import jwt as pyjwt
from bson import ObjectId
from fastapi.testclient import TestClient

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/")

from app.config import AppSettings
from app.dependencies import get_api_key_service, get_url_service
from app.routes.api_v1 import router as api_v1_router
from app.routes.auth import router as auth_router
from app.schemas.models.api_key import ApiKeyDoc
from tests.conftest import build_test_app

# ── Helpers ──────────────────────────────────────────────────────────────────

_settings = AppSettings()
_jwt_cfg = _settings.jwt

_USER_ID = str(ObjectId())


def _make_token(
    scopes: list[str] | None = None,
    app_id: str | None = "spoo-cli",
    email_verified: bool = True,
) -> str:
    """Real access token; scp/app_id only when provided (session otherwise)."""
    now = int(time.time())
    payload = {
        "sub": _USER_ID,
        "iss": _jwt_cfg.jwt_issuer,
        "aud": _jwt_cfg.jwt_audience,
        "iat": now,
        "exp": now + 900,
        "email": "test@example.com",
        "email_verified": email_verified,
        "amr": ["ext"],
    }
    if scopes is not None:
        payload["scp"] = scopes
        payload["app_id"] = app_id
    return pyjwt.encode(payload, _jwt_cfg.jwt_secret, algorithm="HS256")


def _make_legacy_app_token(
    app_id: str = "spoo-cli", email_verified: bool = True
) -> str:
    """Legacy-grant app token: app_id present, no scp claim (unrestricted).

    Mints the shape a pre-scopes grant produces so tests can prove it is
    still treated as a delegated credential, not an interactive session.
    """
    now = int(time.time())
    payload = {
        "sub": _USER_ID,
        "iss": _jwt_cfg.jwt_issuer,
        "aud": _jwt_cfg.jwt_audience,
        "iat": now,
        "exp": now + 900,
        "email": "test@example.com",
        "email_verified": email_verified,
        "amr": ["ext"],
        "app_id": app_id,
    }
    return pyjwt.encode(payload, _jwt_cfg.jwt_secret, algorithm="HS256")


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _empty_url_list_service() -> AsyncMock:
    svc = AsyncMock()
    svc.list_by_owner.return_value = {
        "items": [],
        "page": 1,
        "pageSize": 20,
        "total": 0,
        "hasNext": False,
        "sortBy": "created_at",
        "sortOrder": "desc",
    }
    return svc


def _make_key_doc() -> ApiKeyDoc:
    from datetime import datetime, timezone

    return ApiKeyDoc(
        **{
            "_id": ObjectId(),
            "user_id": ObjectId(_USER_ID),
            "token_prefix": "AbCdEfGh",
            "token_hash": "x" * 64,
            "name": "Test Key",
            "scopes": ["shorten:create"],
            "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
            "revoked": False,
        }
    )


# ── Scope enforcement on API routes ──────────────────────────────────────────


class TestScopeEnforcement:
    def test_in_scope_route_200(self):
        app = build_test_app(
            api_v1_router,
            overrides={get_url_service: _empty_url_list_service},
        )
        token = _make_token(scopes=["urls:read"])
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/urls", headers=_auth(token))
        assert resp.status_code == 200

    def test_out_of_scope_route_403(self):
        app = build_test_app(
            api_v1_router,
            overrides={get_url_service: _empty_url_list_service},
        )
        token = _make_token(scopes=["shorten:create"])
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/urls", headers=_auth(token))
        assert resp.status_code == 403
        assert "scope" in resp.json()["error"].lower()

    def test_empty_scp_is_fully_restricted(self):
        app = build_test_app(
            api_v1_router,
            overrides={get_url_service: _empty_url_list_service},
        )
        token = _make_token(scopes=[])
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/urls", headers=_auth(token))
        assert resp.status_code == 403

    def test_session_token_unrestricted(self):
        app = build_test_app(
            api_v1_router,
            overrides={get_url_service: _empty_url_list_service},
        )
        token = _make_token(scopes=None)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/urls", headers=_auth(token))
        assert resp.status_code == 200


# ── Interactive-only surfaces reject app tokens ──────────────────────────────


class TestInteractiveOnlySurfaces:
    def test_app_token_403_on_apps_list(self):
        app = build_test_app(api_v1_router)
        token = _make_token(scopes=["shorten:create", "admin:all"])
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/apps", headers=_auth(token))
        assert resp.status_code == 403

    def test_app_token_403_on_profile_update(self):
        app = build_test_app(auth_router)
        token = _make_token(scopes=["admin:all"])
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.patch(
                "/auth/me", json={"user_name": "Evil"}, headers=_auth(token)
            )
        assert resp.status_code == 403

    def test_app_token_403_on_device_revoke(self):
        app = build_test_app(auth_router)
        token = _make_token(scopes=["admin:all"])
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/auth/device/revoke",
                json={"app_id": "spoo-cli"},
                headers={**_auth(token), "X-Requested-With": "fetch"},
            )
        assert resp.status_code == 403

    def test_legacy_app_token_403_on_profile_update(self):
        """A scp-less app token (legacy grant) must not pass as a session."""
        app = build_test_app(auth_router)
        token = _make_legacy_app_token()
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.patch(
                "/auth/me", json={"user_name": "Evil"}, headers=_auth(token)
            )
        assert resp.status_code == 403


# ── Key management via keys:manage ───────────────────────────────────────────


class TestKeysManageScope:
    def _keys_service(self) -> AsyncMock:
        svc = AsyncMock()
        key_doc = _make_key_doc()
        svc.create.return_value = (key_doc, "spoo_rawtoken123")
        svc.list_by_user.return_value = [key_doc]
        svc.revoke.return_value = True
        return svc

    def test_app_token_with_keys_manage_lists_keys(self):
        app = build_test_app(
            api_v1_router, overrides={get_api_key_service: self._keys_service}
        )
        token = _make_token(scopes=["keys:manage"])
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/keys", headers=_auth(token))
        assert resp.status_code == 200
        assert len(resp.json()["keys"]) == 1

    def test_app_token_cannot_create_key(self):
        """Minting a credential is first-party only — app tokens get 403.

        Even with keys:manage, an app token cannot create a key (that would
        launder a revocable credential into one that survives grant revoke).
        """
        app = build_test_app(
            api_v1_router, overrides={get_api_key_service: self._keys_service}
        )
        token = _make_token(scopes=["keys:manage"])
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/api/v1/keys",
                json={"name": "CLI Key", "scopes": ["shorten:create"]},
                headers=_auth(token),
            )
        assert resp.status_code == 403

    def test_app_token_with_keys_manage_deletes_key(self):
        app = build_test_app(
            api_v1_router, overrides={get_api_key_service: self._keys_service}
        )
        token = _make_token(scopes=["keys:manage"])
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.delete(f"/api/v1/keys/{ObjectId()}", headers=_auth(token))
        assert resp.status_code == 200

    def test_app_token_without_keys_manage_403(self):
        app = build_test_app(
            api_v1_router, overrides={get_api_key_service: self._keys_service}
        )
        token = _make_token(scopes=["shorten:create", "urls:read"])
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/keys", headers=_auth(token))
        assert resp.status_code == 403

    def test_admin_all_does_not_grant_keys_manage(self):
        app = build_test_app(
            api_v1_router, overrides={get_api_key_service: self._keys_service}
        )
        token = _make_token(scopes=["admin:all"])
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/keys", headers=_auth(token))
        assert resp.status_code == 403

    def test_session_token_still_manages_keys(self):
        app = build_test_app(
            api_v1_router, overrides={get_api_key_service: self._keys_service}
        )
        token = _make_token(scopes=None)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/keys", headers=_auth(token))
        assert resp.status_code == 200

    def test_unverified_session_cannot_create_key(self):
        app = build_test_app(
            api_v1_router, overrides={get_api_key_service: self._keys_service}
        )
        token = _make_token(scopes=None, email_verified=False)  # session
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/api/v1/keys",
                json={"name": "CLI Key", "scopes": ["shorten:create"]},
                headers=_auth(token),
            )
        assert resp.status_code == 403
        assert resp.json()["code"] == "EMAIL_NOT_VERIFIED"

    def test_legacy_app_token_cannot_manage_keys(self):
        """A pre-scopes app token (app_id, no scp) is delegated, not a session."""
        app = build_test_app(
            api_v1_router, overrides={get_api_key_service: self._keys_service}
        )
        token = _make_legacy_app_token()
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/keys", headers=_auth(token))
        assert resp.status_code == 403

    def test_api_key_cannot_be_created_with_keys_manage(self):
        """keys:manage is not in ALLOWED_SCOPES — creation is a 422."""
        app = build_test_app(
            api_v1_router, overrides={get_api_key_service: self._keys_service}
        )
        token = _make_token(scopes=None)  # interactive session
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/api/v1/keys",
                json={"name": "Sneaky", "scopes": ["keys:manage"]},
                headers=_auth(token),
            )
        assert resp.status_code == 422
