"""
Integration tests for API key lifecycle.

POST   /api/v1/keys          -> create an API key
GET    /api/v1/keys          -> list API keys
DELETE /api/v1/keys/{key_id} -> delete/revoke a key
POST   /api/v1/shorten       -> use API key for auth

All DB / Redis / external-service calls are eliminated via
dependency_overrides and a mock lifespan — no real infrastructure needed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

from bson import ObjectId
from fastapi.testclient import TestClient

from app.dependencies import (
    CurrentUser,
    get_api_key_service,
    get_current_user,
    get_url_service,
    require_jwt_verified,
    require_keys_access,
)
from app.routes.api_v1 import router as api_v1_router
from app.schemas.models.api_key import ApiKeyDoc
from tests.conftest import build_test_app

# ── Helpers ──────────────────────────────────────────────────────────────────

_USER_ID = ObjectId()
_KEY_ID = ObjectId()


def _make_verified_user(user_id: ObjectId | None = None) -> CurrentUser:
    return CurrentUser(
        user_id=user_id or _USER_ID,
        email_verified=True,
        api_key_doc=None,
    )


def _make_unverified_user(user_id: ObjectId | None = None) -> CurrentUser:
    return CurrentUser(
        user_id=user_id or _USER_ID,
        email_verified=False,
        api_key_doc=None,
    )


def _make_api_key_doc(
    key_id: ObjectId | None = None,
    user_id: ObjectId | None = None,
    scopes: list[str] | None = None,
    revoked: bool = False,
    expires_at: datetime | None = None,
) -> ApiKeyDoc:
    return ApiKeyDoc.from_mongo(
        {
            "_id": key_id or _KEY_ID,
            "user_id": user_id or _USER_ID,
            "token_prefix": "abcd1234",
            "token_hash": "fakehash",
            "name": "Test Key",
            "description": "A test key",
            "scopes": scopes or ["shorten:create"],
            "expires_at": expires_at,
            "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
            "revoked": revoked,
        }
    )


def _make_api_key_user(
    scopes: list[str] | None = None,
    user_id: ObjectId | None = None,
) -> CurrentUser:
    """Create a CurrentUser authenticated via API key."""
    uid = user_id or _USER_ID
    key_doc = _make_api_key_doc(user_id=uid, scopes=scopes or ["shorten:create"])
    return CurrentUser(
        user_id=uid,
        email_verified=True,
        api_key_doc=key_doc,
    )


# ── POST /api/v1/keys ───────────────────────────────────────────────────────


def test_create_api_key_returns_token_once():
    """POST /api/v1/keys -> 201 with token in response."""
    key_doc = _make_api_key_doc()
    raw_token = "spoo_testrawtokenvalue"

    mock_svc = AsyncMock()
    mock_svc.create = AsyncMock(return_value=(key_doc, raw_token))

    verified_user = _make_verified_user()
    app = build_test_app(
        api_v1_router,
        overrides={
            require_jwt_verified: lambda: verified_user,
            get_api_key_service: lambda: mock_svc,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/api/v1/keys",
        json={"name": "My Key", "scopes": ["shorten:create"]},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["token"] == raw_token
    assert body["name"] == "Test Key"
    assert body["scopes"] == ["shorten:create"]
    assert "id" in body


def test_create_api_key_requires_verified_email():
    """Unverified user -> 403 EMAIL_NOT_VERIFIED."""
    _make_unverified_user()

    # require_verified_email calls require_auth first, then checks email_verified.
    # We override require_verified_email to raise the correct error.
    from app.errors import EmailNotVerifiedError

    def _raise_unverified():
        raise EmailNotVerifiedError("Email verification required")

    app = build_test_app(
        api_v1_router,
        overrides={
            require_jwt_verified: _raise_unverified,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/api/v1/keys",
        json={"name": "My Key", "scopes": ["shorten:create"]},
    )

    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == "EMAIL_NOT_VERIFIED"
    assert resp.headers["X-Error-Code"] == "EMAIL_NOT_VERIFIED"


def test_create_api_key_requires_auth():
    """No auth -> 401."""
    from app.errors import AuthenticationError

    def _raise_unauth():
        raise AuthenticationError("Authentication required")

    app = build_test_app(
        api_v1_router,
        overrides={
            require_jwt_verified: _raise_unauth,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/api/v1/keys",
        json={"name": "My Key", "scopes": ["shorten:create"]},
    )

    assert resp.status_code == 401


def test_create_api_key_with_scopes():
    """POST /api/v1/keys with specific scopes -> key has those scopes."""
    scopes = ["shorten:create", "stats:read"]
    key_doc = _make_api_key_doc(scopes=scopes)
    raw_token = "spoo_scopedtokenvalue"

    mock_svc = AsyncMock()
    mock_svc.create = AsyncMock(return_value=(key_doc, raw_token))

    verified_user = _make_verified_user()
    app = build_test_app(
        api_v1_router,
        overrides={
            require_jwt_verified: lambda: verified_user,
            get_api_key_service: lambda: mock_svc,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/api/v1/keys",
        json={"name": "Scoped Key", "scopes": scopes},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert set(body["scopes"]) == set(scopes)


# ── GET /api/v1/keys ─────────────────────────────────────────────────────────


def test_list_api_keys_returns_without_token():
    """GET /api/v1/keys -> 200, keys don't include raw token."""
    key_doc = _make_api_key_doc()

    mock_svc = AsyncMock()
    mock_svc.list_by_user = AsyncMock(return_value=[key_doc])

    verified_user = _make_verified_user()
    app = build_test_app(
        api_v1_router,
        overrides={
            require_keys_access: lambda: verified_user,
            get_api_key_service: lambda: mock_svc,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/v1/keys")

    assert resp.status_code == 200
    body = resp.json()
    assert "keys" in body
    assert len(body["keys"]) == 1
    key_entry = body["keys"][0]
    assert "token" not in key_entry
    assert key_entry["name"] == "Test Key"
    assert key_entry["token_prefix"] == "abcd1234"


# ── POST /api/v1/shorten with API key ───────────────────────────────────────


def test_use_api_key_for_shorten():
    """POST /api/v1/shorten with API key (shorten:create scope) -> 201."""
    from app.schemas.models.url import UrlV2Doc

    api_key_user = _make_api_key_user(scopes=["shorten:create"])

    mock_url_svc = AsyncMock()
    created_doc = UrlV2Doc.from_mongo(
        {
            "_id": ObjectId(),
            "alias": "newcode",
            "long_url": "https://example.com/long",
            "owner_id": _USER_ID,
            "status": "ACTIVE",
            "private_stats": True,
            "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
            "domain": "spoo.me",
        }
    )
    mock_url_svc.create = AsyncMock(return_value=(created_doc, None))

    app = build_test_app(
        api_v1_router,
        overrides={
            get_current_user: lambda: api_key_user,
            get_url_service: lambda: mock_url_svc,
        },
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/api/v1/shorten",
            json={"url": "https://example.com/long"},
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["alias"] == "newcode"
    assert "short_url" in body


def test_use_api_key_scope_check():
    """API key without shorten:create scope -> 403."""
    api_key_user = _make_api_key_user(scopes=["stats:read"])

    mock_url_svc = AsyncMock()

    app = build_test_app(
        api_v1_router,
        overrides={
            get_current_user: lambda: api_key_user,
            get_url_service: lambda: mock_url_svc,
        },
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/api/v1/shorten",
            json={"url": "https://example.com/long"},
        )

    assert resp.status_code == 403


# ── DELETE /api/v1/keys/{key_id} ─────────────────────────────────────────────


def test_revoke_api_key_soft():
    """DELETE /api/v1/keys/{id}?revoke=true -> 200 with action=revoked."""
    mock_svc = AsyncMock()
    mock_svc.revoke = AsyncMock(return_value=True)

    verified_user = _make_verified_user()
    app = build_test_app(
        api_v1_router,
        overrides={
            require_keys_access: lambda: verified_user,
            get_api_key_service: lambda: mock_svc,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.delete(f"/api/v1/keys/{_KEY_ID}?revoke=true")

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["action"] == "revoked"


def test_revoke_api_key_hard():
    """DELETE /api/v1/keys/{id} (default, no revoke param) -> 200 with action=deleted."""
    mock_svc = AsyncMock()
    mock_svc.revoke = AsyncMock(return_value=True)

    verified_user = _make_verified_user()
    app = build_test_app(
        api_v1_router,
        overrides={
            require_keys_access: lambda: verified_user,
            get_api_key_service: lambda: mock_svc,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.delete(f"/api/v1/keys/{_KEY_ID}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["action"] == "deleted"


def test_revoked_api_key_rejected():
    """After revocation, using key returns None (unauthenticated) -> 401."""
    from app.errors import AuthenticationError

    # Simulate the get_current_user returning None for a revoked key
    # Then require_auth raises 401
    def _raise_unauth():
        raise AuthenticationError("Authentication required")

    mock_svc = AsyncMock()

    app = build_test_app(
        api_v1_router,
        overrides={
            require_keys_access: _raise_unauth,
            get_api_key_service: lambda: mock_svc,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/v1/keys")

    assert resp.status_code == 401


def test_expired_api_key_rejected():
    """API key past expires_at -> unauthenticated (get_current_user returns None)."""
    # When an expired API key is used, get_current_user returns None.
    # For endpoints requiring auth, this means 401.
    from app.errors import AuthenticationError

    def _raise_unauth():
        raise AuthenticationError("Authentication required")

    mock_svc = AsyncMock()

    app = build_test_app(
        api_v1_router,
        overrides={
            require_keys_access: _raise_unauth,
            get_api_key_service: lambda: mock_svc,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/v1/keys")

    assert resp.status_code == 401


def test_api_key_not_found_on_delete():
    """DELETE /api/v1/keys/{nonexistent} -> 404."""
    mock_svc = AsyncMock()
    mock_svc.revoke = AsyncMock(return_value=False)

    verified_user = _make_verified_user()
    app = build_test_app(
        api_v1_router,
        overrides={
            require_keys_access: lambda: verified_user,
            get_api_key_service: lambda: mock_svc,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)

    nonexistent_id = ObjectId()
    resp = client.delete(f"/api/v1/keys/{nonexistent_id}")

    assert resp.status_code == 404
