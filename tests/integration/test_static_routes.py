"""Integration tests for static, SEO, legal, contact, and report routes."""

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

from config import AppSettings
from dependencies import get_contact_service, get_url_service
from errors import AppError, ForbiddenError, ValidationError
from middleware.error_handler import register_error_handlers
from middleware.rate_limiter import limiter
from routes.static_routes import router as static_router

_STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "static"
)


def _mock_contact_service(send_ok=True, captcha_ok=True):
    svc = MagicMock()
    svc.send_contact_message = AsyncMock()
    svc.send_report = AsyncMock()
    if not captcha_ok:
        svc.send_contact_message = AsyncMock(
            side_effect=ForbiddenError("Invalid captcha, please try again")
        )
        svc.send_report = AsyncMock(
            side_effect=ForbiddenError("Invalid captcha, please try again")
        )
    if not send_ok:
        svc.send_contact_message = AsyncMock(
            side_effect=AppError("Error sending message, please try again later")
        )
    return svc


def _mock_url_service(alias_available=True):
    svc = MagicMock()
    svc.check_alias_available = AsyncMock(return_value=alias_available)
    return svc


def _build_test_app(contact_svc=None, url_svc=None):
    settings = AppSettings(
        contact_webhook="https://test.webhook/contact",
        url_report_webhook="https://test.webhook/report",
        hcaptcha_sitekey="test-sitekey",
        hcaptcha_secret="test-secret",
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.db = MagicMock()
        app.state.redis = None
        app.state.email_provider = MagicMock()
        app.state.http_client = MagicMock()
        app.state.oauth_providers = {}
        # Singleton service defaults for dependency lookups
        app.state.contact_service = AsyncMock()
        app.state.url_service = AsyncMock()
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    register_error_handlers(app)
    if os.path.isdir(_STATIC_DIR):
        app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    app.include_router(static_router)
    if contact_svc:
        app.dependency_overrides[get_contact_service] = lambda: contact_svc
    if url_svc:
        app.dependency_overrides[get_url_service] = lambda: url_svc
    return app


# ── SEO files ────────────────────────────────────────────────────────────────


def test_robots_txt():
    app = _build_test_app()
    with TestClient(app) as c:
        resp = c.get("/robots.txt")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]


def test_sitemap_xml():
    app = _build_test_app()
    with TestClient(app) as c:
        resp = c.get("/sitemap.xml")
    assert resp.status_code == 200
    assert "xml" in resp.headers["content-type"]


def test_humans_txt():
    app = _build_test_app()
    with TestClient(app) as c:
        resp = c.get("/humans.txt")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]


def test_security_txt():
    app = _build_test_app()
    with TestClient(app) as c:
        resp = c.get("/security.txt")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]


def test_favicon_ico():
    app = _build_test_app()
    with TestClient(app) as c:
        resp = c.get("/favicon.ico")
    assert resp.status_code == 200


# ── Docs redirects ───────────────────────────────────────────────────────────


def test_docs_redirects_to_external():
    app = _build_test_app()
    with TestClient(app, follow_redirects=False) as c:
        resp = c.get("/docs")
    assert resp.status_code == 301
    assert resp.headers["location"] == "https://spoo.me/docs"


def test_docs_wildcard_redirects():
    app = _build_test_app()
    with TestClient(app, follow_redirects=False) as c:
        resp = c.get("/docs/some-topic")
    assert resp.status_code == 301
    assert resp.headers["location"] == "https://spoo.me/docs/some-topic"


# ── Legal pages ──────────────────────────────────────────────────────────────


def test_privacy_policy_renders():
    app = _build_test_app()
    with TestClient(app) as c:
        resp = c.get("/privacy-policy")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_privacy_policy_alias():
    app = _build_test_app()
    with TestClient(app) as c:
        resp = c.get("/legal/privacy-policy")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_terms_of_service_renders():
    app = _build_test_app()
    with TestClient(app) as c:
        resp = c.get("/terms-of-service")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_terms_of_service_alias():
    app = _build_test_app()
    with TestClient(app) as c:
        resp = c.get("/tos")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_refund_policy_renders():
    app = _build_test_app()
    with TestClient(app) as c:
        resp = c.get("/refund")
    assert resp.status_code == 200
    assert "Paddle is our Merchant of Record" in resp.text


def test_refund_policy_alias():
    app = _build_test_app()
    with TestClient(app) as c:
        resp = c.get("/legal/refund-policy")
    assert resp.status_code == 200


def test_acceptable_use_policy_renders():
    app = _build_test_app()
    with TestClient(app) as c:
        resp = c.get("/acceptable-use")
    assert resp.status_code == 200
    assert "Every destination is checked" in resp.text


def test_acceptable_use_policy_alias():
    app = _build_test_app()
    with TestClient(app) as c:
        resp = c.get("/aup")
    assert resp.status_code == 200


# ── Contact ──────────────────────────────────────────────────────────────────


def test_contact_get_renders_form():
    app = _build_test_app()
    with TestClient(app) as c:
        resp = c.get("/contact")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_contact_post_missing_captcha_returns_400():
    svc = _mock_contact_service()
    app = _build_test_app(contact_svc=svc)
    with TestClient(app) as c:
        resp = c.post("/contact", data={"email": "a@b.com", "message": "hi"})
    assert resp.status_code == 400
    assert "text/html" in resp.headers["content-type"]


def test_contact_post_missing_fields_returns_400():
    svc = _mock_contact_service()
    app = _build_test_app(contact_svc=svc)
    with TestClient(app) as c:
        resp = c.post("/contact", data={"h-captcha-response": "tok"})
    assert resp.status_code == 400


def test_contact_post_success():
    svc = _mock_contact_service()
    app = _build_test_app(contact_svc=svc)
    with TestClient(app) as c:
        resp = c.post(
            "/contact",
            data={"email": "a@b.com", "message": "hello", "h-captcha-response": "tok"},
        )
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    svc.send_contact_message.assert_called_once()


def test_contact_post_captcha_fail_returns_400():
    svc = _mock_contact_service(captcha_ok=False)
    app = _build_test_app(contact_svc=svc)
    with TestClient(app) as c:
        resp = c.post(
            "/contact",
            data={"email": "a@b.com", "message": "hello", "h-captcha-response": "bad"},
        )
    assert resp.status_code == 400
    assert "text/html" in resp.headers["content-type"]


# ── Report ───────────────────────────────────────────────────────────────────


def test_report_get_renders_form():
    app = _build_test_app()
    with TestClient(app) as c:
        resp = c.get("/report")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_report_post_missing_captcha_returns_400():
    svc = _mock_contact_service()
    url_svc = _mock_url_service(alias_available=False)
    app = _build_test_app(contact_svc=svc, url_svc=url_svc)
    with TestClient(app) as c:
        resp = c.post("/report", data={"short_code": "abc", "reason": "spam"})
    assert resp.status_code == 400


def test_report_post_nonexistent_url_returns_400():
    svc = _mock_contact_service()
    svc.send_report = AsyncMock(
        side_effect=ValidationError("Invalid short code, short code does not exist")
    )
    url_svc = _mock_url_service(
        alias_available=True
    )  # alias available = URL doesn't exist
    app = _build_test_app(contact_svc=svc, url_svc=url_svc)
    with TestClient(app) as c:
        resp = c.post(
            "/report",
            data={
                "short_code": "noexist",
                "reason": "spam",
                "h-captcha-response": "tok",
            },
        )
    assert resp.status_code == 400


def test_report_post_success():
    svc = _mock_contact_service()
    url_svc = _mock_url_service(alias_available=False)  # URL exists
    app = _build_test_app(contact_svc=svc, url_svc=url_svc)
    with TestClient(app) as c:
        resp = c.post(
            "/report",
            data={
                "short_code": "abc123",
                "reason": "spam",
                "h-captcha-response": "tok",
            },
        )
    assert resp.status_code == 200
    svc.send_report.assert_called_once()


def test_the_legacy_api_docs_page_is_gone():
    """?old=1 used to render a full copy of the pre-Mintlify docs: indexable,
    self-titled, and competing with the real docs for the same queries."""
    app = _build_test_app()
    with TestClient(app, follow_redirects=False) as c:
        resp = c.get("/api?old=1")
    assert resp.status_code == 301
    assert resp.headers["location"] == "https://spoo.me/docs/introduction"
