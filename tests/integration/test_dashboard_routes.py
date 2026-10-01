"""Integration tests for dashboard routes."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/")

from bson import ObjectId

from app.config import AppSettings
from app.dependencies import (
    CurrentUser,
    get_current_user,
    get_profile_picture_service,
)
from app.errors import NotFoundError
from app.middleware.error_handler import register_error_handlers
from app.middleware.rate_limiter import limiter
from app.routes.dashboard_routes import router as dashboard_router
from app.services.profile_picture_service import ProfilePictureService

_STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "app", "static"
)

_TEST_USER = CurrentUser(user_id=ObjectId(), email_verified=True)

_PROFILE = {
    "id": str(_TEST_USER.user_id),
    "email": "user@example.com",
    "email_verified": True,
    "user_name": "testuser",
    "plan": "free",
    "password_set": True,
    "auth_providers": [],
}


def _mock_svc(profile=None, pictures=None):
    svc = MagicMock(spec=ProfilePictureService)
    svc.get_dashboard_profile = AsyncMock(return_value=profile or _PROFILE)
    svc.get_available_pictures = AsyncMock(return_value=pictures or [])
    svc.set_picture = AsyncMock()
    return svc


def _build_test_app(user=None, svc=None):
    settings = AppSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.db = MagicMock()
        app.state.redis = None
        app.state.email_provider = MagicMock()
        app.state.http_client = MagicMock()
        app.state.oauth_providers = {}
        yield

    app = FastAPI(lifespan=lifespan)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    register_error_handlers(app)
    if os.path.isdir(_STATIC_DIR):
        app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    app.include_router(dashboard_router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_profile_picture_service] = lambda: svc or _mock_svc()
    return app


# ── Unauthenticated redirects ────────────────────────────────────────────────


def test_dashboard_root_unauth_redirects():
    app = _build_test_app(user=None)
    with TestClient(app, follow_redirects=False) as c:
        resp = c.get("/dashboard")
    assert resp.status_code == 302
    assert "login=true" in resp.headers["location"]


def test_dashboard_links_unauth_redirects():
    app = _build_test_app(user=None)
    with TestClient(app, follow_redirects=False) as c:
        resp = c.get("/dashboard/links")
    assert resp.status_code == 302
    assert "login=true" in resp.headers["location"]


# ── Authenticated page rendering ─────────────────────────────────────────────


def test_dashboard_root_auth_redirects_to_links():
    app = _build_test_app(user=_TEST_USER)
    with TestClient(app, follow_redirects=False) as c:
        resp = c.get("/dashboard")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/dashboard/links"


def test_dashboard_links_renders_html():
    app = _build_test_app(user=_TEST_USER)
    with TestClient(app) as c:
        resp = c.get("/dashboard/links")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_dashboard_keys_renders_html():
    app = _build_test_app(user=_TEST_USER)
    with TestClient(app) as c:
        resp = c.get("/dashboard/keys")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_dashboard_statistics_renders_html():
    app = _build_test_app(user=_TEST_USER)
    with TestClient(app) as c:
        resp = c.get("/dashboard/statistics")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_dashboard_settings_renders_html():
    app = _build_test_app(user=_TEST_USER)
    with TestClient(app) as c:
        resp = c.get("/dashboard/settings")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_dashboard_billing_renders_html():
    app = _build_test_app(user=_TEST_USER)
    with TestClient(app) as c:
        resp = c.get("/dashboard/billing")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


# ── Profile pictures ─────────────────────────────────────────────────────────


def test_profile_pictures_get_unauth_returns_401():
    app = _build_test_app(user=None)
    with TestClient(app) as c:
        resp = c.get("/dashboard/profile-pictures")
    assert resp.status_code == 401


def test_profile_pictures_get_returns_json():
    pics = [
        {
            "id": "google_uid",
            "url": "https://pic.com/a.jpg",
            "source": "google",
            "is_current": True,
        }
    ]
    svc = _mock_svc(pictures=pics)
    app = _build_test_app(user=_TEST_USER, svc=svc)
    with TestClient(app) as c:
        resp = c.get("/dashboard/profile-pictures")
    assert resp.status_code == 200
    assert resp.json()["pictures"] == pics


def test_profile_pictures_post_unauth_returns_401():
    app = _build_test_app(user=None)
    with TestClient(app) as c:
        resp = c.post("/dashboard/profile-pictures", json={"picture_id": "x"})
    assert resp.status_code == 401


def test_profile_pictures_post_missing_id_returns_422():
    app = _build_test_app(user=_TEST_USER)
    with TestClient(app) as c:
        resp = c.post("/dashboard/profile-pictures", json={})
    assert resp.status_code == 422


def test_profile_pictures_post_valid_returns_success():
    svc = _mock_svc()
    app = _build_test_app(user=_TEST_USER, svc=svc)
    with TestClient(app) as c:
        resp = c.post(
            "/dashboard/profile-pictures", json={"picture_id": "google_uid123"}
        )
    assert resp.status_code == 200
    svc.set_picture.assert_called_once()


def test_profile_pictures_post_invalid_id_returns_404():
    svc = _mock_svc()
    svc.set_picture = AsyncMock(side_effect=NotFoundError("Picture not found"))
    app = _build_test_app(user=_TEST_USER, svc=svc)
    with TestClient(app) as c:
        resp = c.post("/dashboard/profile-pictures", json={"picture_id": "bad_id"})
    assert resp.status_code == 404


def test_profile_picture_endpoints_reject_api_key_auth():
    """The whole account-profile surface is JWT-only, matching PATCH /auth/me."""
    svc = _mock_svc()
    api_key_user = CurrentUser(
        user_id=ObjectId(), email_verified=True, api_key_doc=MagicMock()
    )
    app = _build_test_app(user=api_key_user, svc=svc)
    with TestClient(app, raise_server_exceptions=False) as c:
        assert c.get("/dashboard/profile-pictures").status_code == 403
        assert (
            c.post("/dashboard/profile-pictures", json={"picture_id": "x"}).status_code
            == 403
        )
    svc.set_picture.assert_not_called()
