"""
Integration tests for OAuth flows: initiate, callback, link, unlink, providers.

All DB / Redis / external-service calls are eliminated via
dependency_overrides and a mock lifespan — no real infrastructure needed.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from bson import ObjectId
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/")

from app.config import AppSettings
from app.dependencies import (
    CurrentUser,
    get_oauth_service,
    require_auth,
)
from app.errors import ValidationError
from app.middleware.error_handler import register_error_handlers
from app.middleware.rate_limiter import limiter
from app.routes.auth import router as auth_router
from app.routes.oauth_routes import router as oauth_router
from app.schemas.models.user import ProviderInfo, UserDoc
from app.schemas.results import AuthResult

# ── Helpers ──────────────────────────────────────────────────────────────────

_USER_OID = ObjectId()
_EMAIL = "oauth@example.com"
_ACCESS_TOKEN = "mock.access.token"
_REFRESH_TOKEN = "mock.refresh.token"


def _build_test_app(
    overrides: dict,
    oauth_providers: dict | None = None,
) -> FastAPI:
    settings = AppSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.db = MagicMock()
        app.state.redis = None
        app.state.email_provider = MagicMock()
        app.state.http_client = MagicMock()
        app.state.oauth_providers = oauth_providers or {}
        yield

    app = FastAPI(lifespan=lifespan)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    register_error_handlers(app)
    app.include_router(auth_router)
    app.include_router(oauth_router)
    app.dependency_overrides.update(overrides)
    return app


def _make_user_doc(
    user_id: ObjectId | None = None,
    email: str = _EMAIL,
    email_verified: bool = True,
    password_set: bool = True,
    auth_providers: list | None = None,
) -> UserDoc:
    oid = user_id or _USER_OID
    providers = auth_providers or []
    return UserDoc.from_mongo(
        {
            "_id": oid,
            "email": email,
            "email_verified": email_verified,
            "password_hash": "$argon2..." if password_set else None,
            "password_set": password_set,
            "user_name": "OAuth User",
            "pfp": None,
            "auth_providers": providers,
            "plan": "free",
            "signup_ip": None,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
            "status": "ACTIVE",
        }
    )


def _mock_user() -> CurrentUser:
    return CurrentUser(user_id=_USER_OID, email_verified=True)


def _make_google_provider_entry() -> dict:
    return {
        "provider": "google",
        "provider_user_id": "google-123",
        "email": _EMAIL,
        "email_verified": True,
        "profile": {"name": "OAuth User", "picture": "https://example.com/pic.jpg"},
        "linked_at": datetime.now(timezone.utc).isoformat(),
    }


def _make_valid_state(provider: str = "google", action: str = "login") -> str:
    """Generate a valid OAuth state string that will pass verify_oauth_state.

    Returns a URL-encoded state string: the raw state contains '&' separators
    which would be misinterpreted as query parameter delimiters if not encoded.
    Uses Z suffix instead of +00:00 to avoid '+' being decoded as space.
    """
    from urllib.parse import quote

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    raw = f"provider={provider}&action={action}&nonce=test123&timestamp={ts}"
    return quote(raw, safe="")


# ── Tests ────────────────────────────────────────────────────────────────────


def test_oauth_initiate_redirects_to_provider():
    """GET /oauth/google -> redirect to provider (302)."""
    mock_oauth_client = AsyncMock()
    # authorize_redirect returns a RedirectResponse
    from fastapi.responses import RedirectResponse

    mock_oauth_client.authorize_redirect.return_value = RedirectResponse(
        "https://accounts.google.com/o/oauth2/auth?...", status_code=302
    )

    mock_strategy = MagicMock()

    app = _build_test_app(
        overrides={},
        oauth_providers={"google": mock_oauth_client},
    )

    with (
        patch("app.routes.oauth_routes.PROVIDER_STRATEGIES", {"google": mock_strategy}),
        TestClient(
            app, raise_server_exceptions=False, follow_redirects=False
        ) as client,
    ):
        resp = client.get("/oauth/google")

    assert resp.status_code == 302
    assert "accounts.google.com" in resp.headers.get("location", "")


def test_oauth_callback_new_user_creates_account():
    """Callback with new user -> 302 redirect to dashboard, cookies set."""
    user = _make_user_doc(email_verified=True, password_set=False)

    mock_oauth_svc = AsyncMock()
    mock_oauth_svc.handle_callback.return_value = AuthResult(
        user=user, access_token=_ACCESS_TOKEN, refresh_token=_REFRESH_TOKEN
    )

    mock_strategy = MagicMock()
    mock_strategy.fetch_user_info = AsyncMock(
        return_value=ProviderInfo(
            email=_EMAIL,
            email_verified=True,
            provider_user_id="google-123",
            name="OAuth User",
            picture="",
        )
    )

    mock_oauth_client = AsyncMock()
    mock_oauth_client.authorize_access_token.return_value = {
        "access_token": "provider.token"
    }

    state = _make_valid_state("google", "login")

    app = _build_test_app(
        overrides={get_oauth_service: lambda: mock_oauth_svc},
        oauth_providers={"google": mock_oauth_client},
    )

    with (
        patch("app.routes.oauth_routes.PROVIDER_STRATEGIES", {"google": mock_strategy}),
        TestClient(
            app, raise_server_exceptions=False, follow_redirects=False
        ) as client,
    ):
        resp = client.get(f"/oauth/google/callback?state={state}&code=auth_code")

    assert resp.status_code == 302
    assert "/dashboard" in resp.headers.get("location", "")
    # Cookies should be set
    set_cookie_headers = resp.headers.get_list("set-cookie")
    cookie_names = [h.split("=")[0] for h in set_cookie_headers]
    assert "access_token" in cookie_names


def test_oauth_callback_existing_user_logs_in():
    """Callback for existing OAuth user -> redirect with tokens."""
    user = _make_user_doc()

    mock_oauth_svc = AsyncMock()
    mock_oauth_svc.handle_callback.return_value = AuthResult(
        user=user, access_token=_ACCESS_TOKEN, refresh_token=_REFRESH_TOKEN
    )

    mock_strategy = MagicMock()
    mock_strategy.fetch_user_info = AsyncMock(
        return_value=ProviderInfo(
            email=_EMAIL,
            email_verified=True,
            provider_user_id="google-123",
            name="OAuth User",
            picture="",
        )
    )

    mock_oauth_client = AsyncMock()
    mock_oauth_client.authorize_access_token.return_value = {
        "access_token": "provider.token"
    }

    state = _make_valid_state("google", "login")

    app = _build_test_app(
        overrides={get_oauth_service: lambda: mock_oauth_svc},
        oauth_providers={"google": mock_oauth_client},
    )

    with (
        patch("app.routes.oauth_routes.PROVIDER_STRATEGIES", {"google": mock_strategy}),
        TestClient(
            app, raise_server_exceptions=False, follow_redirects=False
        ) as client,
    ):
        resp = client.get(f"/oauth/google/callback?state={state}&code=auth_code")

    assert resp.status_code == 302
    mock_oauth_svc.handle_callback.assert_called_once()


def test_oauth_callback_email_collision_auto_links():
    """Callback where email exists but provider not linked, email_verified -> auto-link."""
    user = _make_user_doc(email_verified=True)

    mock_oauth_svc = AsyncMock()
    # handle_callback still succeeds — it handles auto-linking internally
    mock_oauth_svc.handle_callback.return_value = AuthResult(
        user=user, access_token=_ACCESS_TOKEN, refresh_token=_REFRESH_TOKEN
    )

    mock_strategy = MagicMock()
    mock_strategy.fetch_user_info = AsyncMock(
        return_value=ProviderInfo(
            email=_EMAIL,
            email_verified=True,
            provider_user_id="google-new-456",
            name="OAuth User",
            picture="",
        )
    )

    mock_oauth_client = AsyncMock()
    mock_oauth_client.authorize_access_token.return_value = {
        "access_token": "provider.token"
    }

    state = _make_valid_state("google", "login")

    app = _build_test_app(
        overrides={get_oauth_service: lambda: mock_oauth_svc},
        oauth_providers={"google": mock_oauth_client},
    )

    with (
        patch("app.routes.oauth_routes.PROVIDER_STRATEGIES", {"google": mock_strategy}),
        TestClient(
            app, raise_server_exceptions=False, follow_redirects=False
        ) as client,
    ):
        resp = client.get(f"/oauth/google/callback?state={state}&code=auth_code")

    assert resp.status_code == 302
    # Verify the service was called with the correct provider info
    call_args = mock_oauth_svc.handle_callback.call_args
    assert call_args[0][0] == "google"
    assert call_args[0][1].email == _EMAIL


def test_oauth_link_requires_auth():
    """GET /oauth/google/link without auth -> 401."""
    mock_oauth_client = AsyncMock()
    mock_strategy = MagicMock()

    app = _build_test_app(
        overrides={},
        oauth_providers={"google": mock_oauth_client},
    )

    with (
        patch("app.routes.oauth_routes.PROVIDER_STRATEGIES", {"google": mock_strategy}),
        TestClient(app, raise_server_exceptions=False) as client,
    ):
        resp = client.get("/oauth/google/link")

    assert resp.status_code == 401


def test_oauth_link_initiates_for_authenticated_user():
    """Authenticated user -> GET /oauth/google/link -> redirect to provider."""
    from fastapi.responses import RedirectResponse

    mock_oauth_client = AsyncMock()
    mock_oauth_client.authorize_redirect.return_value = RedirectResponse(
        "https://accounts.google.com/o/oauth2/auth?link=true", status_code=302
    )

    mock_strategy = MagicMock()
    mock_user = _mock_user()

    app = _build_test_app(
        overrides={require_auth: lambda: mock_user},
        oauth_providers={"google": mock_oauth_client},
    )

    with (
        patch("app.routes.oauth_routes.PROVIDER_STRATEGIES", {"google": mock_strategy}),
        TestClient(
            app, raise_server_exceptions=False, follow_redirects=False
        ) as client,
    ):
        resp = client.get("/oauth/google/link")

    assert resp.status_code == 302
    assert "accounts.google.com" in resp.headers.get("location", "")


def test_oauth_unlink_provider_success():
    """DELETE /oauth/providers/google/unlink -> 200."""
    mock_oauth_svc = AsyncMock()
    mock_oauth_svc.unlink_provider.return_value = None
    mock_user = _mock_user()

    app = _build_test_app(
        {
            get_oauth_service: lambda: mock_oauth_svc,
            require_auth: lambda: mock_user,
        }
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.delete("/oauth/providers/google/unlink")

    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert "google" in resp.json()["message"]


def test_oauth_unlink_last_method_fails():
    """Cannot unlink last auth method -> 400."""
    mock_oauth_svc = AsyncMock()
    mock_oauth_svc.unlink_provider.side_effect = ValidationError(
        "cannot unlink last authentication method"
    )
    mock_user = _mock_user()

    app = _build_test_app(
        {
            get_oauth_service: lambda: mock_oauth_svc,
            require_auth: lambda: mock_user,
        }
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.delete("/oauth/providers/google/unlink")

    assert resp.status_code == 400


def test_oauth_list_providers_returns_linked():
    """GET /oauth/providers -> list of linked providers."""
    mock_oauth_svc = AsyncMock()
    mock_oauth_svc.list_providers.return_value = (
        [
            {
                "provider": "google",
                "email": _EMAIL,
                "email_verified": True,
                "linked_at": None,
                "profile": {"name": "OAuth User", "picture": ""},
            },
            {
                "provider": "github",
                "email": _EMAIL,
                "email_verified": True,
                "linked_at": None,
                "profile": {"name": "GH User", "picture": ""},
            },
        ],
        True,  # password_set
    )
    mock_user = _mock_user()

    app = _build_test_app(
        {
            get_oauth_service: lambda: mock_oauth_svc,
            require_auth: lambda: mock_user,
        }
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/oauth/providers")

    assert resp.status_code == 200
    data = resp.json()
    assert data["password_set"] is True
    assert len(data["providers"]) == 2
    provider_names = {p["provider"] for p in data["providers"]}
    assert provider_names == {"google", "github"}


def test_oauth_unconfigured_provider_404():
    """GET /oauth/nonexistent -> 404."""
    app = _build_test_app(overrides={})
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/oauth/nonexistent")

    assert resp.status_code == 404


def test_oauth_callback_missing_state_returns_400():
    """Callback without state param -> 400."""
    mock_oauth_svc = AsyncMock()
    mock_strategy = MagicMock()
    mock_oauth_client = AsyncMock()

    app = _build_test_app(
        overrides={get_oauth_service: lambda: mock_oauth_svc},
        oauth_providers={"google": mock_oauth_client},
    )

    with (
        patch("app.routes.oauth_routes.PROVIDER_STRATEGIES", {"google": mock_strategy}),
        TestClient(app, raise_server_exceptions=False) as client,
    ):
        resp = client.get("/oauth/google/callback")

    assert resp.status_code == 400


def test_oauth_callback_provider_error_returns_400():
    """Callback with ?error=access_denied from provider -> 400."""
    mock_oauth_svc = AsyncMock()
    mock_strategy = MagicMock()
    mock_oauth_client = AsyncMock()

    state = _make_valid_state("google", "login")

    app = _build_test_app(
        overrides={get_oauth_service: lambda: mock_oauth_svc},
        oauth_providers={"google": mock_oauth_client},
    )

    with (
        patch("app.routes.oauth_routes.PROVIDER_STRATEGIES", {"google": mock_strategy}),
        TestClient(app, raise_server_exceptions=False) as client,
    ):
        resp = client.get(
            f"/oauth/google/callback?state={state}&error=access_denied"
            "&error_description=User+denied+access"
        )

    assert resp.status_code == 400
    assert "OAuth error" in resp.json().get("error", "")
