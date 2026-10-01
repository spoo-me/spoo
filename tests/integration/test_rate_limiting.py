"""
Integration tests for rate limiting infrastructure.

Tests verify that slowapi rate limiting triggers 429 responses,
content negotiation works on 429, and key resolution behaves correctly.
Each test builds a fresh FastAPI app with a dedicated test route to
guarantee deterministic limit behaviour.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/")

import pytest
from bson import ObjectId

from config import AppSettings
from dependencies import Entitled, get_current_user
from dependencies.auth import CurrentUser
from middleware.error_handler import register_error_handlers
from middleware.rate_limiter import (
    Limits,
    RateLimitHeadersMiddleware,
    limiter,
    plan_scaled,
    rate_limit_key,
)
from routes.health_routes import router as health_router
from services.entitlements import for_plan
from services.features.catalog import Plan

_STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "static"
)


# ── Helpers ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_limiter_between_tests():
    """Clear all in-memory rate limit counters before AND after each test."""
    limiter.reset()
    yield
    limiter.reset()


def _reset_limiter() -> None:
    """Clear all in-memory rate limit counters between tests."""
    limiter.reset()


def _build_test_app(extra_routers: list | None = None) -> FastAPI:
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
    app.add_middleware(RateLimitHeadersMiddleware)
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    register_error_handlers(app)
    if os.path.isdir(_STATIC_DIR):
        app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    for r in extra_routers or []:
        app.include_router(r)
    return app


def _make_limited_router(limit_str: str = "2/minute", prefix: str = "") -> APIRouter:
    """Create a test router with a single rate-limited GET endpoint."""
    r = APIRouter(prefix=prefix)

    @r.get("/test-limited")
    @limiter.limit(limit_str)
    async def _limited(request: Request):
        return {"ok": True}

    return r


def _make_api_limited_router(limit_str: str = "2/minute") -> APIRouter:
    """Create a test router under /api/v1 prefix with rate limiting."""
    r = APIRouter(prefix="/api/v1")

    @r.get("/test-limited")
    @limiter.limit(limit_str)
    async def _api_limited(request: Request):
        return {"ok": True}

    return r


# ── Tests ────────────────────────────────────────────────────────────────────


def test_rate_limit_returns_429_after_exceeded():
    """Making more requests than the limit should trigger 429."""
    _reset_limiter()
    router = _make_limited_router("2/minute")
    app = _build_test_app(extra_routers=[router])
    with TestClient(app, raise_server_exceptions=False) as c:
        r1 = c.get("/test-limited")
        r2 = c.get("/test-limited")
        r3 = c.get("/test-limited")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 429


def test_rate_limit_json_response_on_api_route():
    """429 on /api/v1/* should return JSON with error and code keys."""
    _reset_limiter()
    router = _make_api_limited_router("1/minute")
    app = _build_test_app(extra_routers=[router])
    with TestClient(app, raise_server_exceptions=False) as c:
        c.get("/api/v1/test-limited")
        resp = c.get("/api/v1/test-limited")
    assert resp.status_code == 429
    data = resp.json()
    assert "error" in data
    assert "code" in data
    assert data["code"] == "rate_limit_exceeded"
    assert resp.headers["X-Error-Code"] == "rate_limit_exceeded"
    assert "application/json" in resp.headers["content-type"]


def test_rate_limit_html_response_on_page_route():
    """429 on a non-API route (no /api/, /auth/, /oauth/ prefix) should return HTML."""
    _reset_limiter()
    router = _make_limited_router("1/minute", prefix="/page")
    app = _build_test_app(extra_routers=[router])
    with TestClient(app, raise_server_exceptions=False) as c:
        c.get("/page/test-limited")
        resp = c.get("/page/test-limited")
    assert resp.status_code == 429
    assert "text/html" in resp.headers["content-type"]
    assert resp.headers["X-Error-Code"] == "rate_limit_exceeded"


def test_rate_limit_html_edge_composed_returns_empty_body(edge_composed_errors):
    """Flag on: the HTML 429 skips the template — Caddy composes the page."""
    _reset_limiter()
    router = _make_limited_router("1/minute", prefix="/page")
    app = _build_test_app(extra_routers=[router])
    with TestClient(app, raise_server_exceptions=False) as c:
        c.get("/page/test-limited")
        resp = c.get("/page/test-limited")
    assert resp.status_code == 429
    assert resp.headers["X-Error-Code"] == "rate_limit_exceeded"
    assert resp.content == b""


def test_rate_limit_hint_on_anonymous_api_route():
    """Anonymous 429 on /api/v1/* carries the authenticate hint."""
    _reset_limiter()
    router = _make_api_limited_router("1/minute")
    app = _build_test_app(extra_routers=[router])
    with TestClient(app, raise_server_exceptions=False) as c:
        c.get("/api/v1/test-limited")
        resp = c.get("/api/v1/test-limited")
    assert resp.status_code == 429
    assert "free account" in resp.json()["hint"]


def test_rate_limit_hint_absent_for_authenticated_api_route():
    """Authenticated callers already have the top tier — no hint."""
    _reset_limiter()
    router = _make_api_limited_router("1/minute")
    app = _build_test_app(extra_routers=[router])
    headers = {"Authorization": "Bearer spoo_testkey123"}
    with TestClient(app, raise_server_exceptions=False) as c:
        c.get("/api/v1/test-limited", headers=headers)
        resp = c.get("/api/v1/test-limited", headers=headers)
    assert resp.status_code == 429
    assert "hint" not in resp.json()


def test_rate_limit_hint_on_legacy_shorten():
    """429 on the legacy mint endpoints points at API v1."""
    _reset_limiter()
    r = APIRouter()

    @r.post("/emoji")
    @limiter.limit("1/minute")
    async def _emoji(request: Request):
        return {"ok": True}

    app = _build_test_app(extra_routers=[r])
    headers = {"Accept": "application/json"}
    with TestClient(app, raise_server_exceptions=False) as c:
        c.post("/emoji", headers=headers)
        resp = c.post("/emoji", headers=headers)
    assert resp.status_code == 429
    assert "/api/v1/shorten" in resp.json()["hint"]
    assert "/api/v1/shorten" in resp.headers["X-Spoo-Hint"]


def test_rate_limit_hint_header_on_legacy_form_post():
    """A form post with curl's default Accept gets HTML, so the hint must
    ride a header to reach the common legacy caller."""
    _reset_limiter()
    r = APIRouter()

    @r.post("/emoji")
    @limiter.limit("1/minute")
    async def _emoji(request: Request):
        return {"ok": True}

    app = _build_test_app(extra_routers=[r])
    with TestClient(app, raise_server_exceptions=False) as c:
        c.post("/emoji", data={"url": "https://example.com"})
        resp = c.post("/emoji", data={"url": "https://example.com"})
    assert resp.status_code == 429
    assert "text/html" in resp.headers["content-type"]
    assert "/api/v1/shorten" in resp.headers["X-Spoo-Hint"]


def test_hint_prose_matches_authed_tier():
    """The anonymous hint hardcodes the authed numbers as prose; fail loudly
    when the tier changes so the string can't silently lie."""
    assert Limits.API_AUTHED == "60 per minute; 5000 per day"


def test_rate_limit_key_uses_ip_for_anonymous():
    """Anonymous requests (no auth header, no cookie) should be keyed by client IP."""
    _reset_limiter()
    # Build a minimal mock request
    mock_request = MagicMock()
    mock_request.headers = {}
    mock_request.cookies = {}
    mock_request.client = MagicMock()
    mock_request.client.host = "192.168.1.100"

    key = rate_limit_key(mock_request)
    # For anonymous, key should be the IP address
    assert key == "192.168.1.100"


def test_rate_limit_key_uses_jwt_hash_for_bearer_token():
    """Bearer <jwt> should produce a jwt:-prefixed key."""
    mock_request = MagicMock()
    mock_request.headers = {"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.test"}
    mock_request.cookies = {}

    key = rate_limit_key(mock_request)
    assert key.startswith("jwt:")


def test_rate_limit_key_uses_apikey_hash_for_spoo_token():
    """Bearer spoo_<raw> should produce an apikey:-prefixed key."""
    mock_request = MagicMock()
    mock_request.headers = {"Authorization": "Bearer spoo_abc123secret"}
    mock_request.cookies = {}

    key = rate_limit_key(mock_request)
    assert key.startswith("apikey:")


def test_rate_limit_key_uses_cookie_jwt_when_no_header():
    """access_token cookie should produce a jwt:-prefixed key."""
    mock_request = MagicMock()
    mock_request.headers = {}
    mock_request.cookies = {"access_token": "eyJhbGciOiJIUzI1NiJ9.cookie"}

    key = rate_limit_key(mock_request)
    assert key.startswith("jwt:")


def test_health_endpoint_not_rate_limited():
    """The /health endpoint should not be subject to rate limiting."""
    _reset_limiter()
    mock_db = MagicMock()
    mock_db.client.admin.command = AsyncMock(return_value={"ok": 1})

    @asynccontextmanager
    async def lifespan(a: FastAPI):
        a.state.settings = AppSettings()
        a.state.db = mock_db
        a.state.redis = None
        a.state.email_provider = MagicMock()
        a.state.http_client = MagicMock()
        a.state.oauth_providers = {}
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.limiter = limiter
    app.add_middleware(RateLimitHeadersMiddleware)
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    register_error_handlers(app)
    if os.path.isdir(_STATIC_DIR):
        app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    app.include_router(health_router)

    with TestClient(app, raise_server_exceptions=False) as c:
        # Make many requests — all should succeed (not rate limited)
        statuses = [c.get("/health").status_code for _ in range(15)]
    # All should be 200 (healthy with mock DB) — none should be 429
    assert 429 not in statuses
    assert all(s == 200 for s in statuses)


def test_rate_limit_different_ips_have_separate_buckets():
    """Different client IPs should have independent rate limit counters."""
    _reset_limiter()
    # Use a unique endpoint to avoid any cross-test state leakage
    r = APIRouter()

    @r.get("/test-ip-buckets")
    @limiter.limit("2/minute")
    async def _ip_bucket_test(request: Request):
        return {"ok": True}

    app = _build_test_app(extra_routers=[r])
    with TestClient(app, raise_server_exceptions=False) as c:
        # First IP — 2 requests (should both succeed under 2/minute)
        resp1a = c.get(
            "/test-ip-buckets",
            headers={"X-Forwarded-For": "10.0.0.1"},
        )
        resp1b = c.get(
            "/test-ip-buckets",
            headers={"X-Forwarded-For": "10.0.0.1"},
        )
        # Second IP — first request should succeed (separate bucket)
        resp2 = c.get(
            "/test-ip-buckets",
            headers={"X-Forwarded-For": "10.0.0.2"},
        )
        # First IP — third request should be blocked
        resp1c = c.get(
            "/test-ip-buckets",
            headers={"X-Forwarded-For": "10.0.0.1"},
        )
    assert resp1a.status_code == 200
    assert resp1b.status_code == 200
    assert resp2.status_code == 200  # separate bucket
    assert resp1c.status_code == 429  # exceeded for first IP


# ── Rate limit headers ───────────────────────────────────────────────────────


def test_successful_response_carries_rate_limit_headers():
    """Decorated endpoints should expose X-RateLimit-* headers on 200s.

    Uses a uniquely named endpoint: the shared limiter registers limits by
    function name, so reusing the helper's endpoint would accumulate limits
    from other tests in this module.
    """
    _reset_limiter()
    r = APIRouter()

    @r.get("/test-headers-ok")
    @limiter.limit("5/minute")
    async def _headers_ok_probe(request: Request):
        return {"ok": True}

    app = _build_test_app(extra_routers=[r])
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/test-headers-ok")
        assert resp.status_code == 200
        assert resp.headers.get("X-RateLimit-Limit") == "5"
        assert resp.headers.get("X-RateLimit-Remaining") == "4"
        # Integer epoch seconds — slowapi emits a float, the middleware casts
        assert resp.headers["X-RateLimit-Reset"].isdigit()
        # Retry-After on a success would trip generic client backoff layers
        assert "Retry-After" not in resp.headers


def test_429_carries_rate_limit_and_retry_after_headers():
    """The custom 429 handler should carry Retry-After and X-RateLimit-*."""
    _reset_limiter()
    r = APIRouter()

    @r.get("/test-headers-429")
    @limiter.limit("1/minute")
    async def _headers_429_probe(request: Request):
        return {"ok": True}

    app = _build_test_app(extra_routers=[r])
    with TestClient(app, raise_server_exceptions=False) as c:
        c.get("/test-headers-429")
        resp = c.get("/test-headers-429", headers={"Accept": "application/json"})
        assert resp.status_code == 429
        assert resp.json()["code"] == "rate_limit_exceeded"
        assert resp.headers["Retry-After"].isdigit()
        assert resp.headers.get("X-RateLimit-Limit") == "1"
        assert resp.headers.get("X-RateLimit-Remaining") == "0"
        assert resp.headers["X-RateLimit-Reset"].isdigit()


def test_response_endpoints_get_single_header_set():
    """Endpoints returning a Response object must not get duplicate headers.

    The middleware is the single injection point; the decorator-side
    injection is a no-op even when a Response is available to mutate.
    """
    _reset_limiter()
    r = APIRouter()

    @r.get("/test-headers-response")
    @limiter.limit("5/minute")
    async def _headers_response_probe(request: Request):
        return JSONResponse({"ok": True})

    app = _build_test_app(extra_routers=[r])
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/test-headers-response")
        assert resp.status_code == 200
        assert resp.headers.get_list("X-RateLimit-Limit") == ["5"]
        assert resp.headers.get_list("X-RateLimit-Remaining") == ["4"]
        reset_values = resp.headers.get_list("X-RateLimit-Reset")
        assert len(reset_values) == 1 and reset_values[0].isdigit()
        assert "Retry-After" not in resp.headers


# ── Plan multiplier ──────────────────────────────────────────────────────────


_scaled_router = APIRouter(prefix="/api/v1")


# Registered once: slowapi keys route limits by function name, so a second
# decoration of the same name would count every request twice.
@_scaled_router.get("/test-scaled")
@limiter.limit(plan_scaled("2/minute"))
async def _scaled(request: Request, entitlements: Entitled):
    return {"plan": entitlements.plan.value}


def _plan_scaled_app(plan):
    app = _build_test_app(extra_routers=[_scaled_router])
    user = CurrentUser(user_id=ObjectId(), email_verified=True)
    app.dependency_overrides[get_current_user] = lambda: user
    ent = AsyncMock()
    ent.resolve_for = AsyncMock(return_value=for_plan(Plan(plan)))
    app.state.entitlement_service = ent
    return app


def _hit(client, n: int, token: str) -> list[int]:
    return [
        client.get(
            "/api/v1/test-scaled", headers={"Authorization": f"Bearer {token}"}
        ).status_code
        for _ in range(n)
    ]


def test_pro_multiplier_scales_the_route_limit_through_the_real_dependency():
    _reset_limiter()
    token = str(ObjectId())
    with TestClient(_plan_scaled_app("pro")) as client:
        assert _hit(client, 10, token) == [200] * 10
        assert _hit(client, 1, token) == [429]


def test_free_plan_keeps_the_base_limit():
    _reset_limiter()
    token = str(ObjectId())
    with TestClient(_plan_scaled_app("free")) as client:
        assert _hit(client, 3, token) == [200, 200, 429]
