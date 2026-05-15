"""Integration tests for the device auth flow endpoints."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from bson import ObjectId
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/")

from config import AppSettings
from dependencies import (
    get_app_grant_repo,
    get_credential_service,
    get_current_user,
    get_device_auth_service,
    get_user_repo,
)
from dependencies.auth import CurrentUser
from errors import AuthenticationError
from middleware.error_handler import register_error_handlers
from middleware.rate_limiter import limiter
from routes.auth import router as auth_router
from schemas.models.app import AppEntry, AppStatus, AppType
from schemas.models.app_grant import AppGrantDoc
from schemas.models.user import UserDoc
from schemas.results import AuthResult

_USER_OID = ObjectId()
_EMAIL = "test@example.com"
_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

_TEST_APP_REGISTRY: dict[str, AppEntry] = {
    "spoo-snap": AppEntry(
        name="Spoo Snap",
        icon="spoo-snap.svg",
        description="Official browser extension",
        verified=True,
        status=AppStatus.LIVE,
        type=AppType.DEVICE_AUTH,
        redirect_uris=[],
        permissions=["Access your account"],
    ),
    "spoo-future": AppEntry(
        name="Future App",
        icon="future.svg",
        description="Not yet released",
        verified=True,
        status=AppStatus.COMING_SOON,
        type=AppType.DEVICE_AUTH,
    ),
}


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_user_doc() -> UserDoc:
    return UserDoc.from_mongo(
        {
            "_id": _USER_OID,
            "email": _EMAIL,
            "email_verified": True,
            "password_set": True,
            "auth_providers": [],
            "plan": "free",
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
            "status": "ACTIVE",
        }
    )


def _make_grant(app_id: str = "spoo-snap") -> AppGrantDoc:
    return AppGrantDoc.from_mongo(
        {
            "_id": ObjectId(),
            "user_id": _USER_OID,
            "app_id": app_id,
            "granted_at": datetime.now(timezone.utc),
            "last_used_at": None,
            "revoked_at": None,
        }
    )


def _resolve_app(app_id: str) -> AppEntry | None:
    """Mirror real DeviceAuthService.resolve_app using test registry."""
    entry = _TEST_APP_REGISTRY.get(app_id)
    return entry if entry and entry.is_live_device_app() else None


@pytest.fixture()
def device_auth_svc():
    svc = AsyncMock()
    # resolve_app and validate_redirect_uri are sync — must not be coroutines
    svc.resolve_app = MagicMock(side_effect=_resolve_app)
    svc.validate_redirect_uri = MagicMock(
        side_effect=lambda uri, app: (
            not uri
            or any(
                uri.startswith(a[:-1]) if a.endswith("*") else uri == a
                for a in app.redirect_uris
            )
        )
    )
    return svc


@pytest.fixture()
def credential_svc():
    return AsyncMock()


@pytest.fixture()
def user_repo_mock():
    repo = AsyncMock()
    repo.find_by_id.return_value = _make_user_doc()
    return repo


@pytest.fixture()
def grant_repo():
    repo = AsyncMock()
    repo.find_active_grant.return_value = None
    repo.create_or_reactivate.return_value = MagicMock()
    repo.touch_last_used.return_value = None
    return repo


@pytest.fixture()
def anon_user():
    return None


@pytest.fixture()
def authed_user():
    return CurrentUser(user_id=_USER_OID, email_verified=True)


@pytest.fixture()
def _app_factory():
    """Returns a factory that builds a TestClient with given mocks."""
    _clients: list[TestClient] = []

    def _make(
        device_auth_svc, credential_svc, user_repo_mock, grant_repo, user=None
    ) -> TestClient:
        settings = AppSettings()

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            app.state.settings = settings
            app.state.db = MagicMock()
            app.state.redis = None
            app.state.email_provider = MagicMock()
            app.state.http_client = MagicMock()
            app.state.oauth_providers = {}
            app.state.app_registry = _TEST_APP_REGISTRY
            yield

        app = FastAPI(lifespan=lifespan)
        app.state.limiter = limiter
        app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
        register_error_handlers(app)

        static_dir = os.path.join(_PROJECT_ROOT, "static")
        if os.path.isdir(static_dir):
            app.mount("/static", StaticFiles(directory=static_dir), name="static")

        app.include_router(auth_router)
        app.dependency_overrides[get_device_auth_service] = lambda: device_auth_svc
        app.dependency_overrides[get_credential_service] = lambda: credential_svc
        app.dependency_overrides[get_user_repo] = lambda: user_repo_mock
        app.dependency_overrides[get_app_grant_repo] = lambda: grant_repo
        app.dependency_overrides[get_current_user] = (
            (lambda: user) if user is not None else (lambda: None)
        )

        client = TestClient(app, raise_server_exceptions=False)
        client.__enter__()
        _clients.append(client)
        return client

    yield _make

    for c in _clients:
        c.__exit__(None, None, None)


# ── GET /auth/device/login — validation ──────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "/auth/device/login?state=abc",  # missing app_id
        "/auth/device/login?app_id=unknown-app&state=abc",  # unknown app
        "/auth/device/login?app_id=spoo-future&state=abc",  # coming_soon
    ],
    ids=["missing_app_id", "unknown_app_id", "coming_soon_app"],
)
def test_device_login_invalid_app_returns_400(
    device_auth_svc, credential_svc, user_repo_mock, grant_repo, url, _app_factory
):
    c = _app_factory(device_auth_svc, credential_svc, user_repo_mock, grant_repo)
    resp = c.get(url)
    assert resp.status_code == 400
    assert "Unknown or unsupported" in resp.text


def test_device_login_invalid_redirect_uri(
    device_auth_svc, credential_svc, user_repo_mock, grant_repo, _app_factory
):
    c = _app_factory(device_auth_svc, credential_svc, user_repo_mock, grant_repo)
    resp = c.get(
        "/auth/device/login?app_id=spoo-snap&state=abc&redirect_uri=https://evil.com"
    )
    assert resp.status_code == 400
    assert "Invalid redirect URI" in resp.text


# ── GET /auth/device/login — unauthenticated ─────────────────────────────────


def test_device_login_unauthenticated_redirects(
    device_auth_svc, credential_svc, user_repo_mock, grant_repo, _app_factory
):
    c = _app_factory(device_auth_svc, credential_svc, user_repo_mock, grant_repo)
    resp = c.get(
        "/auth/device/login?app_id=spoo-snap&state=abc", follow_redirects=False
    )
    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert "/?next=" in loc
    assert "spoo-snap" in loc
    assert "abc" in loc


# ── GET /auth/device/login — authenticated ───────────────────────────────────


def test_device_login_with_grant_auto_approves(
    device_auth_svc,
    credential_svc,
    user_repo_mock,
    grant_repo,
    authed_user,
    _app_factory,
):
    device_auth_svc.create_device_auth_code.return_value = "test-code-123"
    grant_repo.find_active_grant.return_value = _make_grant()

    c = _app_factory(
        device_auth_svc, credential_svc, user_repo_mock, grant_repo, authed_user
    )
    resp = c.get(
        "/auth/device/login?app_id=spoo-snap&state=xyz", follow_redirects=False
    )
    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert "/auth/device/callback" in loc
    assert "code=test-code-123" in loc
    assert "state=xyz" in loc


def test_device_login_without_grant_shows_consent(
    device_auth_svc,
    credential_svc,
    user_repo_mock,
    grant_repo,
    authed_user,
    _app_factory,
):
    c = _app_factory(
        device_auth_svc, credential_svc, user_repo_mock, grant_repo, authed_user
    )
    resp = c.get("/auth/device/login?app_id=spoo-snap&state=xyz")
    assert resp.status_code == 200
    assert "Spoo Snap" in resp.text
    assert "Allow" in resp.text
    assert "Connecting as" in resp.text
    assert "csrf_token" in resp.text


# ── GET /auth/device/callback ────────────────────────────────────────────────


def test_device_callback_no_code_redirects(
    device_auth_svc, credential_svc, user_repo_mock, grant_repo, _app_factory
):
    c = _app_factory(device_auth_svc, credential_svc, user_repo_mock, grant_repo)
    resp = c.get("/auth/device/callback", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"


def test_device_callback_with_code_renders(
    device_auth_svc, credential_svc, user_repo_mock, grant_repo, _app_factory
):
    c = _app_factory(device_auth_svc, credential_svc, user_repo_mock, grant_repo)
    resp = c.get("/auth/device/callback?code=abc&state=xyz")
    assert resp.status_code == 200
    assert 'data-code="abc"' in resp.text
    assert 'data-state="xyz"' in resp.text


# ── POST /auth/device/token ──────────────────────────────────────────────────


def test_device_token_valid_code(
    device_auth_svc, credential_svc, user_repo_mock, grant_repo, _app_factory
):
    user = _make_user_doc()
    device_auth_svc.exchange_device_code.return_value = AuthResult(
        user=user, access_token="at", refresh_token="rt", app_id="spoo-snap"
    )
    grant_repo.find_active_grant.return_value = _make_grant()

    c = _app_factory(device_auth_svc, credential_svc, user_repo_mock, grant_repo)
    resp = c.post("/auth/device/token", json={"code": "valid-code"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["access_token"] == "at"
    assert data["refresh_token"] == "rt"
    assert data["user"]["email"] == _EMAIL
    grant_repo.touch_last_used.assert_awaited_once()


def test_device_token_invalid_code(
    device_auth_svc, credential_svc, user_repo_mock, grant_repo, _app_factory
):
    device_auth_svc.exchange_device_code.side_effect = AuthenticationError(
        "invalid or expired"
    )

    c = _app_factory(device_auth_svc, credential_svc, user_repo_mock, grant_repo)
    resp = c.post("/auth/device/token", json={"code": "bad"})
    assert resp.status_code == 401


def test_device_token_revoked_grant_rejected(
    device_auth_svc, credential_svc, user_repo_mock, grant_repo, _app_factory
):
    """Token exchange fails if grant was revoked between consent and exchange."""
    user = _make_user_doc()
    device_auth_svc.exchange_device_code.return_value = AuthResult(
        user=user, access_token="at", refresh_token="rt", app_id="spoo-snap"
    )
    grant_repo.find_active_grant.return_value = None  # revoked

    c = _app_factory(device_auth_svc, credential_svc, user_repo_mock, grant_repo)
    resp = c.post("/auth/device/token", json={"code": "valid"})
    assert resp.status_code == 401
    assert "revoked" in resp.json()["error"].lower()


# ── POST /auth/device/consent ────────────────────────────────────────────────


def test_consent_missing_csrf_rejected(
    device_auth_svc,
    credential_svc,
    user_repo_mock,
    grant_repo,
    authed_user,
    _app_factory,
):
    c = _app_factory(
        device_auth_svc, credential_svc, user_repo_mock, grant_repo, authed_user
    )
    resp = c.post(
        "/auth/device/consent",
        data={"app_id": "spoo-snap", "state": "xyz", "csrf_token": "wrong"},
    )
    assert resp.status_code == 403
    assert "Invalid or expired" in resp.text


def test_consent_valid_creates_grant(
    device_auth_svc,
    credential_svc,
    user_repo_mock,
    grant_repo,
    authed_user,
    _app_factory,
):
    device_auth_svc.create_device_auth_code.return_value = "consent-code-123"

    c = _app_factory(
        device_auth_svc, credential_svc, user_repo_mock, grant_repo, authed_user
    )
    c.cookies.set("_consent_csrf", "valid-tok")
    resp = c.post(
        "/auth/device/consent",
        data={
            "app_id": "spoo-snap",
            "state": "xyz",
            "csrf_token": "valid-tok",
            "redirect_uri": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "consent-code-123" in resp.headers["location"]
    grant_repo.create_or_reactivate.assert_awaited_once()


def test_consent_unknown_app_rejected(
    device_auth_svc,
    credential_svc,
    user_repo_mock,
    grant_repo,
    authed_user,
    _app_factory,
):
    c = _app_factory(
        device_auth_svc, credential_svc, user_repo_mock, grant_repo, authed_user
    )
    c.cookies.set("_consent_csrf", "tok")
    resp = c.post(
        "/auth/device/consent",
        data={"app_id": "unknown", "state": "", "csrf_token": "tok"},
    )
    assert resp.status_code == 400


# ── POST /auth/device/revoke ─────────────────────────────────────────────────


def test_revoke_without_csrf_header_rejected(
    device_auth_svc,
    credential_svc,
    user_repo_mock,
    grant_repo,
    authed_user,
    _app_factory,
):
    c = _app_factory(
        device_auth_svc, credential_svc, user_repo_mock, grant_repo, authed_user
    )
    resp = c.post("/auth/device/revoke", data={"app_id": "spoo-snap"})
    assert resp.status_code == 403


def test_revoke_success(
    device_auth_svc,
    credential_svc,
    user_repo_mock,
    grant_repo,
    authed_user,
    _app_factory,
):
    grant_repo.revoke.return_value = True

    c = _app_factory(
        device_auth_svc, credential_svc, user_repo_mock, grant_repo, authed_user
    )
    resp = c.post(
        "/auth/device/revoke",
        data={"app_id": "spoo-snap"},
        headers={"X-Requested-With": "fetch"},
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    grant_repo.revoke.assert_awaited_once()
    device_auth_svc.revoke_device_tokens.assert_awaited_once()


def test_revoke_no_grant_returns_404(
    device_auth_svc,
    credential_svc,
    user_repo_mock,
    grant_repo,
    authed_user,
    _app_factory,
):
    grant_repo.revoke.return_value = False

    c = _app_factory(
        device_auth_svc, credential_svc, user_repo_mock, grant_repo, authed_user
    )
    resp = c.post(
        "/auth/device/revoke",
        data={"app_id": "spoo-snap"},
        headers={"X-Requested-With": "fetch"},
    )
    assert resp.status_code == 404


# ── POST /auth/device/refresh ─────────────────────────────────────────────────


def test_device_refresh_success(
    device_auth_svc, credential_svc, user_repo_mock, grant_repo, _app_factory
):
    user = _make_user_doc()
    credential_svc.refresh_token.return_value = AuthResult(
        user=user, access_token="new-at", refresh_token="new-rt", app_id="spoo-snap"
    )
    grant_repo.find_active_grant.return_value = _make_grant()

    c = _app_factory(device_auth_svc, credential_svc, user_repo_mock, grant_repo)
    resp = c.post("/auth/device/refresh", json={"refresh_token": "old-rt"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["access_token"] == "new-at"
    assert data["refresh_token"] == "new-rt"
    grant_repo.touch_last_used.assert_awaited_once()


def test_device_refresh_no_app_id_skips_grant_check(
    device_auth_svc, credential_svc, user_repo_mock, grant_repo, _app_factory
):
    user = _make_user_doc()
    credential_svc.refresh_token.return_value = AuthResult(
        user=user, access_token="new-at", refresh_token="new-rt"
    )

    c = _app_factory(device_auth_svc, credential_svc, user_repo_mock, grant_repo)
    resp = c.post("/auth/device/refresh", json={"refresh_token": "old-rt"})
    assert resp.status_code == 200
    grant_repo.find_active_grant.assert_not_awaited()


def test_device_refresh_revoked_grant_rejected(
    device_auth_svc, credential_svc, user_repo_mock, grant_repo, _app_factory
):
    user = _make_user_doc()
    credential_svc.refresh_token.return_value = AuthResult(
        user=user, access_token="new-at", refresh_token="new-rt", app_id="spoo-snap"
    )
    grant_repo.find_active_grant.return_value = None

    c = _app_factory(device_auth_svc, credential_svc, user_repo_mock, grant_repo)
    resp = c.post("/auth/device/refresh", json={"refresh_token": "old-rt"})
    assert resp.status_code == 401
    assert "revoked" in resp.json()["error"].lower()
    grant_repo.touch_last_used.assert_not_awaited()
