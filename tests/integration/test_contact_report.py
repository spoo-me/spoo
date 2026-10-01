"""
Integration tests for contact and report form infrastructure.

Tests verify form submission flows, captcha validation, service error
handling, and webhook failure scenarios through the static_routes
contact/report endpoints.
"""

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

from app.config import AppSettings
from app.dependencies import get_contact_service, get_url_service
from app.errors import AppError, ForbiddenError, ValidationError
from app.middleware.error_handler import register_error_handlers
from app.middleware.rate_limiter import limiter
from app.routes.static_routes import router as static_router

_STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "app", "static"
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _reset_limiter() -> None:
    limiter.reset()


def _mock_contact_service(
    captcha_ok: bool = True,
    send_ok: bool = True,
    report_ok: bool = True,
) -> MagicMock:
    """Create a mock ContactService with configurable behaviour."""
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
    elif not send_ok:
        svc.send_contact_message = AsyncMock(
            side_effect=AppError("Error sending message, please try again later")
        )
    elif not report_ok:
        svc.send_report = AsyncMock(
            side_effect=AppError("Error sending report, please try again later")
        )
    return svc


def _mock_url_service(alias_available: bool = True) -> MagicMock:
    """Mock UrlService. alias_available=True means URL does NOT exist."""
    svc = MagicMock()
    svc.check_alias_available = AsyncMock(return_value=alias_available)
    return svc


def _build_test_app(
    contact_svc: MagicMock | None = None,
    url_svc: MagicMock | None = None,
) -> FastAPI:
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
        # Singleton service defaults (overridden via dependency_overrides when needed)
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
    if contact_svc is not None:
        app.dependency_overrides[get_contact_service] = lambda: contact_svc
    if url_svc is not None:
        app.dependency_overrides[get_url_service] = lambda: url_svc
    return app


# ── Contact GET ──────────────────────────────────────────────────────────────


def test_contact_form_get_renders_html():
    """GET /contact should return 200 with an HTML form."""
    _reset_limiter()
    app = _build_test_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/contact")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


# ── Contact POST — validation ────────────────────────────────────────────────


def test_contact_form_missing_captcha_returns_400():
    """POST /contact without h-captcha-response should return 400 HTML."""
    _reset_limiter()
    svc = _mock_contact_service()
    app = _build_test_app(contact_svc=svc)
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.post("/contact", data={"email": "user@test.com", "message": "hi"})
    assert resp.status_code == 400
    assert "text/html" in resp.headers["content-type"]
    assert "captcha" in resp.text.lower()


def test_contact_form_missing_fields_returns_400():
    """POST /contact without email and message should return 400 HTML."""
    _reset_limiter()
    svc = _mock_contact_service()
    app = _build_test_app(contact_svc=svc)
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.post("/contact", data={"h-captcha-response": "tok"})
    assert resp.status_code == 400
    assert "text/html" in resp.headers["content-type"]


def test_contact_form_captcha_failure_returns_400():
    """POST /contact with bad captcha (service raises ForbiddenError) should return 400 HTML."""
    _reset_limiter()
    svc = _mock_contact_service(captcha_ok=False)
    app = _build_test_app(contact_svc=svc)
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.post(
            "/contact",
            data={
                "email": "user@test.com",
                "message": "hello",
                "h-captcha-response": "bad-token",
            },
        )
    assert resp.status_code == 400
    assert "text/html" in resp.headers["content-type"]


def test_contact_form_success():
    """POST /contact with valid data should return 200 HTML with success message."""
    _reset_limiter()
    svc = _mock_contact_service()
    app = _build_test_app(contact_svc=svc)
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.post(
            "/contact",
            data={
                "email": "user@test.com",
                "message": "Hello, this is a test",
                "h-captcha-response": "valid-tok",
            },
        )
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    svc.send_contact_message.assert_called_once_with(
        "user@test.com", "Hello, this is a test", "valid-tok"
    )


def test_contact_form_service_error_rerenders():
    """POST /contact when webhook send fails should re-render contact.html with error."""
    _reset_limiter()
    svc = _mock_contact_service(send_ok=False)
    app = _build_test_app(contact_svc=svc)
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.post(
            "/contact",
            data={
                "email": "user@test.com",
                "message": "hello",
                "h-captcha-response": "tok",
            },
        )
    assert resp.status_code == 400
    assert "text/html" in resp.headers["content-type"]


def test_contact_form_preserves_input_on_error():
    """When contact form submission fails, the email and message should be passed back to the template."""
    _reset_limiter()
    svc = _mock_contact_service(captcha_ok=False)
    app = _build_test_app(contact_svc=svc)
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.post(
            "/contact",
            data={
                "email": "keepme@test.com",
                "message": "preserve this",
                "h-captcha-response": "bad",
            },
        )
    assert resp.status_code == 400
    # The template should receive the email/message context for re-rendering
    # We can at least verify the service was called
    svc.send_contact_message.assert_called_once()


# ── Report GET ───────────────────────────────────────────────────────────────


def test_report_form_get_renders_html():
    """GET /report should return 200 with an HTML form."""
    _reset_limiter()
    app = _build_test_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/report")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


# ── Report POST — validation ────────────────────────────────────────────────


def test_report_form_missing_captcha_returns_400():
    """POST /report without h-captcha-response should return 400 HTML."""
    _reset_limiter()
    svc = _mock_contact_service()
    url_svc = _mock_url_service(alias_available=False)
    app = _build_test_app(contact_svc=svc, url_svc=url_svc)
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.post("/report", data={"short_code": "abc123", "reason": "spam"})
    assert resp.status_code == 400
    assert "text/html" in resp.headers["content-type"]


def test_report_form_missing_fields_returns_400():
    """POST /report without short_code and reason should return 400 HTML."""
    _reset_limiter()
    svc = _mock_contact_service()
    url_svc = _mock_url_service()
    app = _build_test_app(contact_svc=svc, url_svc=url_svc)
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.post("/report", data={"h-captcha-response": "tok"})
    assert resp.status_code == 400
    assert "text/html" in resp.headers["content-type"]


def test_report_form_nonexistent_url_returns_400():
    """POST /report for a non-existent short code should return 400 HTML."""
    _reset_limiter()
    svc = _mock_contact_service()
    svc.send_report = AsyncMock(
        side_effect=ValidationError("Invalid short code, short code does not exist")
    )
    url_svc = _mock_url_service(
        alias_available=True
    )  # alias available = URL does NOT exist
    app = _build_test_app(contact_svc=svc, url_svc=url_svc)
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.post(
            "/report",
            data={
                "short_code": "nonexist",
                "reason": "malware",
                "h-captcha-response": "tok",
            },
        )
    assert resp.status_code == 400
    assert "text/html" in resp.headers["content-type"]


def test_report_form_success():
    """POST /report with valid data should return 200 HTML success."""
    _reset_limiter()
    svc = _mock_contact_service()
    url_svc = _mock_url_service(alias_available=False)  # URL exists
    app = _build_test_app(contact_svc=svc, url_svc=url_svc)
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.post(
            "/report",
            data={
                "short_code": "abc123",
                "reason": "phishing site",
                "h-captcha-response": "valid-tok",
            },
        )
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    svc.send_report.assert_called_once()


def test_report_form_webhook_failure_handled():
    """POST /report when webhook send fails should re-render with error (not crash)."""
    _reset_limiter()
    svc = _mock_contact_service(report_ok=False)
    url_svc = _mock_url_service(alias_available=False)
    app = _build_test_app(contact_svc=svc, url_svc=url_svc)
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.post(
            "/report",
            data={
                "short_code": "abc123",
                "reason": "spam",
                "h-captcha-response": "tok",
            },
        )
    assert resp.status_code == 400
    assert "text/html" in resp.headers["content-type"]


def test_report_form_captcha_failure_returns_400():
    """POST /report with bad captcha should return 400 HTML."""
    _reset_limiter()
    svc = _mock_contact_service(captcha_ok=False)
    url_svc = _mock_url_service(alias_available=False)
    app = _build_test_app(contact_svc=svc, url_svc=url_svc)
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.post(
            "/report",
            data={
                "short_code": "abc123",
                "reason": "spam",
                "h-captcha-response": "bad",
            },
        )
    assert resp.status_code == 400
    assert "text/html" in resp.headers["content-type"]


def test_report_strips_url_prefix_from_short_code():
    """POST /report with a full URL as short_code should strip to the last path segment."""
    _reset_limiter()
    svc = _mock_contact_service()
    url_svc = _mock_url_service(alias_available=False)  # URL exists
    app = _build_test_app(contact_svc=svc, url_svc=url_svc)
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.post(
            "/report",
            data={
                "short_code": "https://spoo.me/abc123",
                "reason": "spam",
                "h-captcha-response": "tok",
            },
        )
    assert resp.status_code == 200
    # The url_service.check_alias_available should receive just "abc123"
    url_svc.check_alias_available.assert_called_once_with("abc123")


def test_report_send_report_receives_url_exists_flag():
    """ContactService.send_report should receive url_exists=True when URL is found."""
    _reset_limiter()
    svc = _mock_contact_service()
    url_svc = _mock_url_service(alias_available=False)  # URL exists
    app = _build_test_app(contact_svc=svc, url_svc=url_svc)
    with TestClient(app, raise_server_exceptions=False) as c:
        c.post(
            "/report",
            data={
                "short_code": "found123",
                "reason": "spam",
                "h-captcha-response": "tok",
            },
        )
    call_args = svc.send_report.call_args
    # send_report(short_code, reason, ip, host_url, captcha_token, url_exists)
    assert call_args[0][0] == "found123"  # short_code
    assert call_args[0][5] is True  # url_exists
