"""Integration tests for Phase 14 middleware: logging, error handling, security."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from unittest.mock import MagicMock

from fastapi import APIRouter, FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/")

from config import AppSettings
from errors import ForbiddenError, NotFoundError
from middleware.error_handler import register_error_handlers
from middleware.logging import RequestLoggingMiddleware
from middleware.rate_limiter import limiter
from middleware.security import (
    MaxContentLengthMiddleware,
    SecurityHeadersMiddleware,
    configure_cors,
)

_STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "static"
)


def _build_test_app(
    include_logging=True,
    max_content_length=1_048_576,
    hsts_enabled=True,
) -> FastAPI:
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

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.limiter = limiter
    configure_cors(app, settings)
    app.add_middleware(SecurityHeadersMiddleware, hsts_enabled=hsts_enabled)
    app.add_middleware(
        MaxContentLengthMiddleware, max_content_length=max_content_length
    )
    if include_logging:
        app.add_middleware(RequestLoggingMiddleware)
    register_error_handlers(app)
    if os.path.isdir(_STATIC_DIR):
        app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    return app


# ── Test routes for error handler testing ────────────────────────────────────

_test_router = APIRouter()


@_test_router.get("/api/v1/test-404")
async def api_not_found(request: Request):
    raise NotFoundError("resource not found")


@_test_router.get("/page-404")
async def page_not_found(request: Request):
    raise NotFoundError("page not found")


@_test_router.get("/auth/test-401")
async def auth_error(request: Request):
    raise ForbiddenError("forbidden")


@_test_router.get("/test-ok")
async def test_ok(request: Request):
    return {"status": "ok"}


@_test_router.post("/test-body")
async def test_body(request: Request):
    return {"status": "ok"}


# ── Request Logging Tests ────────────────────────────────────────────────────


def test_request_id_in_response_header():
    app = _build_test_app()
    app.include_router(_test_router)
    with TestClient(app) as c:
        resp = c.get("/test-ok")
    assert resp.status_code == 200
    request_id = resp.headers.get("x-request-id")
    assert request_id is not None
    assert request_id.startswith("req_")


def test_request_id_unique_per_request():
    app = _build_test_app()
    app.include_router(_test_router)
    with TestClient(app) as c:
        r1 = c.get("/test-ok")
        r2 = c.get("/test-ok")
    assert r1.headers["x-request-id"] != r2.headers["x-request-id"]


# ── Error Handler Content Negotiation Tests ──────────────────────────────────


def test_api_route_error_returns_json():
    app = _build_test_app()
    app.include_router(_test_router)
    with TestClient(app) as c:
        resp = c.get("/api/v1/test-404")
    assert resp.status_code == 404
    data = resp.json()
    assert data["error"] == "resource not found"
    assert data["code"] == "not_found"


def test_auth_route_error_returns_json():
    app = _build_test_app()
    app.include_router(_test_router)
    with TestClient(app) as c:
        resp = c.get("/auth/test-401")
    assert resp.status_code == 403
    assert "application/json" in resp.headers["content-type"]


def test_page_route_error_returns_html():
    app = _build_test_app()
    app.include_router(_test_router)
    with TestClient(app) as c:
        resp = c.get("/page-404")
    assert resp.status_code == 404
    assert "text/html" in resp.headers["content-type"]


def test_accept_json_header_overrides_to_json():
    """Even a page route returns JSON if Accept: application/json is set."""
    app = _build_test_app()
    app.include_router(_test_router)
    with TestClient(app) as c:
        resp = c.get("/page-404", headers={"Accept": "application/json"})
    assert resp.status_code == 404
    assert "application/json" in resp.headers["content-type"]


# ── Security Middleware Tests ────────────────────────────────────────────────


def test_security_headers_present():
    app = _build_test_app(include_logging=False)
    app.include_router(_test_router)
    with TestClient(app) as c:
        resp = c.get("/test-ok")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert (
        resp.headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"
    )
    assert "content-security-policy-report-only" in resp.headers
    # HSTS enabled by default in test
    assert (
        resp.headers["strict-transport-security"]
        == "max-age=31536000; includeSubDomains"
    )


def test_security_headers_hsts_disabled():
    app = _build_test_app(include_logging=False, hsts_enabled=False)
    app.include_router(_test_router)
    with TestClient(app) as c:
        resp = c.get("/test-ok")
    assert "strict-transport-security" not in resp.headers
    # Other headers still present
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_cors_public_route_allows_any_origin():
    app = _build_test_app(include_logging=False)
    app.include_router(_test_router)
    with TestClient(app) as c:
        resp = c.get(
            "/api/v1/test-404",
            headers={"Origin": "https://evil.com"},
        )
    assert resp.headers.get("access-control-allow-origin") == "*"
    assert "access-control-allow-credentials" not in resp.headers


def test_cors_public_route_preflight():
    app = _build_test_app(include_logging=False)
    app.include_router(_test_router)
    with TestClient(app) as c:
        resp = c.options(
            "/api/v1/test-404",
            headers={
                "Origin": "https://example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert resp.status_code == 204
    assert resp.headers.get("access-control-allow-origin") == "*"
    assert "access-control-max-age" in resp.headers


def test_cors_private_route_allowed_origin():
    app = _build_test_app(include_logging=False)
    # Override private origins for this test
    from middleware.security import SplitCORSMiddleware

    for mw in app.user_middleware:
        if mw.cls is SplitCORSMiddleware:
            mw.kwargs["private_origins"] = ["https://spoo.me"]
    app.include_router(_test_router)
    with TestClient(app, base_url="http://testserver") as c:
        resp = c.get(
            "/auth/test-401",
            headers={"Origin": "https://spoo.me"},
        )
    assert resp.headers.get("access-control-allow-origin") == "https://spoo.me"
    assert resp.headers.get("access-control-allow-credentials") == "true"


def test_cors_private_route_disallowed_origin():
    app = _build_test_app(include_logging=False)
    app.include_router(_test_router)
    with TestClient(app) as c:
        resp = c.get(
            "/auth/test-401",
            headers={"Origin": "https://evil.com"},
        )
    # No CORS headers — browser will block the response
    assert "access-control-allow-origin" not in resp.headers


def test_cors_public_route_exposes_headers():
    app = _build_test_app(include_logging=False)
    app.include_router(_test_router)
    with TestClient(app) as c:
        resp = c.get(
            "/api/v1/test-404",
            headers={"Origin": "https://example.com"},
        )
    assert resp.headers.get("access-control-expose-headers") == (
        "X-Request-ID, X-Error-Code, X-Spoo-Hint, X-RateLimit-Limit, "
        "X-RateLimit-Remaining, X-RateLimit-Reset, Retry-After"
    )


def test_cors_private_route_exposes_headers():
    app = _build_test_app(include_logging=False)
    from middleware.security import SplitCORSMiddleware

    for mw in app.user_middleware:
        if mw.cls is SplitCORSMiddleware:
            mw.kwargs["private_origins"] = ["https://spoo.me"]
    app.include_router(_test_router)
    with TestClient(app, base_url="http://testserver") as c:
        resp = c.get(
            "/auth/test-401",
            headers={"Origin": "https://spoo.me"},
        )
    assert resp.headers.get("access-control-expose-headers") == (
        "X-Request-ID, X-Error-Code, X-Spoo-Hint, X-RateLimit-Limit, "
        "X-RateLimit-Remaining, X-RateLimit-Reset, Retry-After"
    )


def test_cors_no_expose_headers_without_origin():
    app = _build_test_app(include_logging=False)
    app.include_router(_test_router)
    with TestClient(app) as c:
        resp = c.get("/api/v1/test-404")
    assert "access-control-expose-headers" not in resp.headers


def test_cors_no_expose_headers_for_disallowed_private_origin():
    app = _build_test_app(include_logging=False)
    app.include_router(_test_router)
    with TestClient(app) as c:
        resp = c.get(
            "/auth/test-401",
            headers={"Origin": "https://evil.com"},
        )
    assert "access-control-expose-headers" not in resp.headers


def test_cors_unclassified_route_no_cors():
    app = _build_test_app(include_logging=False)
    app.include_router(_test_router)
    with TestClient(app) as c:
        resp = c.get("/test-ok", headers={"Origin": "https://example.com"})
    assert "access-control-allow-origin" not in resp.headers


def test_body_size_limit_rejects_large_payload():
    app = _build_test_app(max_content_length=100)
    app.include_router(_test_router)
    with TestClient(app) as c:
        resp = c.post(
            "/test-body",
            content=b"x" * 200,
            headers={
                "Content-Length": "200",
                "Content-Type": "application/octet-stream",
            },
        )
    assert resp.status_code == 413
    assert resp.json()["code"] == "payload_too_large"
    assert resp.headers["X-Error-Code"] == "payload_too_large"


def test_body_size_limit_allows_normal_payload():
    app = _build_test_app(max_content_length=1000)
    app.include_router(_test_router)
    with TestClient(app) as c:
        resp = c.post(
            "/test-body",
            content=b"x" * 50,
            headers={
                "Content-Length": "50",
                "Content-Type": "application/octet-stream",
            },
        )
    assert resp.status_code == 200
