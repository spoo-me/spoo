"""
Integration tests for global error handling and content negotiation.

Tests verify that AppError subclasses, validation errors, and unhandled
exceptions produce the correct HTTP status codes and response format
(JSON vs HTML) based on request path and Accept header.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/")

from app.config import AppSettings
from app.errors import (
    AuthenticationError,
    BlockedUrlError,
    ConflictError,
    EmailNotVerifiedError,
    ForbiddenError,
    GoneError,
    NotFoundError,
    RateLimitError,
    ValidationError,
)
from app.middleware.error_handler import register_error_handlers
from app.middleware.rate_limiter import limiter

_STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "app", "static"
)

# ── Test routers with controlled error triggers ─────────────────────────────

_api_router = APIRouter(prefix="/api/v1")
_auth_router = APIRouter(prefix="/auth")
_oauth_router = APIRouter(prefix="/oauth")
_page_router = APIRouter()


@_api_router.get("/trigger-not-found")
async def api_not_found(request: Request):
    raise NotFoundError("resource not found")


@_api_router.get("/trigger-forbidden")
async def api_forbidden(request: Request):
    raise ForbiddenError("access denied")


@_api_router.get("/trigger-gone")
async def api_gone(request: Request):
    raise GoneError("resource expired")


@_api_router.get("/trigger-conflict")
async def api_conflict(request: Request):
    raise ConflictError("already exists")


@_api_router.get("/trigger-validation")
async def api_validation(request: Request):
    raise ValidationError("invalid input", field="email")


@_api_router.get("/trigger-unhandled")
async def api_unhandled(request: Request):
    raise RuntimeError("unexpected failure")


@_api_router.get("/trigger-email-not-verified")
async def api_email_not_verified(request: Request):
    raise EmailNotVerifiedError("email not verified")


@_api_router.get("/trigger-teapot")
async def api_teapot(request: Request):
    from starlette.exceptions import HTTPException

    raise HTTPException(status_code=418, detail="teapot")


@_api_router.get("/trigger-422")
async def api_pydantic_422(request: Request, count: int = Depends(lambda: None)):
    """Uses a query param typed as int — providing a non-int triggers RequestValidationError."""
    return {"count": count}


@_auth_router.get("/trigger-auth-error")
async def auth_trigger(request: Request):
    raise AuthenticationError("bad credentials")


@_oauth_router.get("/trigger-oauth-error")
async def oauth_trigger(request: Request):
    raise ForbiddenError("oauth forbidden")


@_page_router.get("/page/trigger-not-found")
async def page_not_found(request: Request):
    raise NotFoundError("page not found")


@_page_router.get("/page/trigger-forbidden")
async def page_forbidden(request: Request):
    raise ForbiddenError("page forbidden")


@_page_router.get("/page/trigger-gone")
async def page_gone(request: Request):
    raise GoneError("page expired")


@_page_router.get("/page/trigger-blocked")
async def page_blocked(request: Request):
    raise BlockedUrlError("page blocked")


@_page_router.get("/page/trigger-rate-limited")
async def page_rate_limited(request: Request):
    raise RateLimitError("too many requests")


@_page_router.get("/page/trigger-unhandled")
async def page_unhandled(request: Request):
    raise RuntimeError("unexpected page failure")


@_page_router.get("/page/trigger-validation")
async def page_validation(request: Request):
    raise ValidationError("bad page input")


@_page_router.post("/page/trigger-not-found-post")
async def page_not_found_post(request: Request):
    raise NotFoundError("page not found")


# ── Helpers ──────────────────────────────────────────────────────────────────


def _build_test_app() -> FastAPI:
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
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    register_error_handlers(app)
    if os.path.isdir(_STATIC_DIR):
        app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    app.include_router(_api_router)
    app.include_router(_auth_router)
    app.include_router(_oauth_router)
    app.include_router(_page_router)
    return app


# ── API route errors → JSON ─────────────────────────────────────────────────


def test_app_error_json_on_api_route():
    """AppError on /api/* should return JSON with error and code keys."""
    app = _build_test_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/api/v1/trigger-not-found")
    assert resp.status_code == 404
    data = resp.json()
    assert data["error"] == "resource not found"
    assert data["code"] == "not_found"
    assert resp.headers["X-Error-Code"] == "not_found"
    assert "application/json" in resp.headers["content-type"]


def test_app_error_json_on_auth_route():
    """AppError on /auth/* should return JSON."""
    app = _build_test_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/auth/trigger-auth-error")
    assert resp.status_code == 401
    data = resp.json()
    assert data["error"] == "bad credentials"
    assert data["code"] == "authentication_error"
    assert resp.headers["X-Error-Code"] == "authentication_error"
    assert "application/json" in resp.headers["content-type"]


def test_app_error_json_on_oauth_route():
    """AppError on /oauth/* should return JSON."""
    app = _build_test_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/oauth/trigger-oauth-error")
    assert resp.status_code == 403
    data = resp.json()
    assert data["error"] == "oauth forbidden"
    assert "application/json" in resp.headers["content-type"]


# ── Page route errors → HTML ────────────────────────────────────────────────


def test_app_error_html_on_page_route():
    """AppError on a page route (no /api/, /auth/, /oauth/ prefix) should return HTML."""
    app = _build_test_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/page/trigger-not-found")
    assert resp.status_code == 404
    assert "text/html" in resp.headers["content-type"]
    assert resp.headers["X-Error-Code"] == "not_found"


def test_app_error_json_when_accept_json():
    """Even a page route should return JSON when Accept: application/json is set."""
    app = _build_test_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get(
            "/page/trigger-not-found",
            headers={"Accept": "application/json"},
        )
    assert resp.status_code == 404
    data = resp.json()
    assert data["error"] == "page not found"
    assert data["code"] == "not_found"
    assert "application/json" in resp.headers["content-type"]


# ── Unmatched routes (framework-raised 404s) ────────────────────────────────


def test_unmatched_route_returns_html():
    """A path matching NO route (nested, deeper than any pattern) must get
    the same negotiated HTML + X-Error-Code as route-raised 404s — not
    Starlette's default {"detail": "Not Found"} JSON."""
    app = _build_test_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/foo/bar", headers={"Accept": "text/html"})
    assert resp.status_code == 404
    assert "text/html" in resp.headers["content-type"]
    assert resp.headers["X-Error-Code"] == "not_found"


def test_unmatched_route_json_when_accept_json():
    app = _build_test_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/foo/bar", headers={"Accept": "application/json"})
    assert resp.status_code == 404
    data = resp.json()
    assert data["code"] == "not_found"
    assert resp.headers["X-Error-Code"] == "not_found"
    assert "detail" not in data


# ── Validation errors ────────────────────────────────────────────────────────


def test_validation_error_returns_422_json():
    """RequestValidationError (single field) should return structured 422 JSON."""
    app = _build_test_app()
    trigger_router = APIRouter(prefix="/api/v1")

    @trigger_router.get("/typed-param")
    async def typed_param(request: Request, count: int):
        return {"count": count}

    app.include_router(trigger_router)
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/api/v1/typed-param?count=not_a_number")
    assert resp.status_code == 422
    data = resp.json()
    assert data["code"] == "validation_error"
    assert resp.headers["X-Error-Code"] == "validation_error"
    assert data["field"] == "count"
    assert data["error"].startswith("count: ")
    assert "details" not in data


def test_validation_error_multi_field_returns_details():
    """RequestValidationError (multiple fields) should include details array."""
    app = _build_test_app()
    trigger_router = APIRouter(prefix="/api/v1")

    @trigger_router.get("/multi-param")
    async def multi_param(request: Request, count: int, name: str):
        return {"count": count, "name": name}

    app.include_router(trigger_router)
    with TestClient(app, raise_server_exceptions=False) as c:
        # Omit both required query params to trigger multiple errors
        resp = c.get("/api/v1/multi-param")
    assert resp.status_code == 422
    data = resp.json()
    assert data["code"] == "validation_error"
    assert "details" in data
    detail_fields = [d["field"] for d in data["details"]]
    assert "count" in detail_fields
    assert "name" in detail_fields
    assert "field" not in data  # top-level field only for single errors


def test_pydantic_validation_error_returns_422():
    """Pydantic ValidationError (raised in business logic) should return structured 422 JSON."""
    from pydantic import BaseModel

    app = _build_test_app()
    trigger_router = APIRouter(prefix="/api/v1")

    @trigger_router.get("/pydantic-error")
    async def pydantic_error(request: Request):
        class StrictModel(BaseModel):
            value: int

        StrictModel(value="not_an_int")  # type: ignore[arg-type]

    app.include_router(trigger_router)
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/api/v1/pydantic-error")
    assert resp.status_code == 422
    data = resp.json()
    assert data["code"] == "validation_error"
    assert data["field"] == "value"
    assert data["error"].startswith("value: ")


# ── Unhandled exceptions ─────────────────────────────────────────────────────


def test_unhandled_exception_returns_500_json():
    """Unknown exception on API route should return 500 JSON."""
    app = _build_test_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/api/v1/trigger-unhandled")
    assert resp.status_code == 500
    data = resp.json()
    assert data["error"] == "An internal server error occurred."
    assert data["code"] == "internal_error"
    assert resp.headers["X-Error-Code"] == "internal_error"


def test_unhandled_exception_returns_500_html():
    """Unknown exception on page route should return 500 HTML."""
    app = _build_test_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/page/trigger-unhandled")
    assert resp.status_code == 500
    assert "text/html" in resp.headers["content-type"]
    assert resp.headers["X-Error-Code"] == "internal_error"


# ── Error shape verification ─────────────────────────────────────────────────


def test_not_found_error_has_correct_shape():
    """NotFoundError → 404 JSON with error and code keys."""
    app = _build_test_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/api/v1/trigger-not-found")
    assert resp.status_code == 404
    data = resp.json()
    assert "error" in data
    assert data["code"] == "not_found"


def test_forbidden_error_has_correct_shape():
    """ForbiddenError → 403 JSON with error and code keys."""
    app = _build_test_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/api/v1/trigger-forbidden")
    assert resp.status_code == 403
    data = resp.json()
    assert "error" in data
    assert data["code"] == "forbidden"


def test_gone_error_has_correct_shape():
    """GoneError → 410 JSON with error and code keys."""
    app = _build_test_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/api/v1/trigger-gone")
    assert resp.status_code == 410
    data = resp.json()
    assert data["error"] == "resource expired"
    assert data["code"] == "gone"


def test_conflict_error_has_correct_shape():
    """ConflictError → 409 JSON with error and code keys."""
    app = _build_test_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/api/v1/trigger-conflict")
    assert resp.status_code == 409
    data = resp.json()
    assert data["error"] == "already exists"
    assert data["code"] == "conflict"


def test_validation_error_includes_field():
    """ValidationError with a field argument should include the field key in JSON."""
    app = _build_test_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/api/v1/trigger-validation")
    assert resp.status_code == 400
    data = resp.json()
    assert data["error"] == "invalid input"
    assert data["code"] == "validation_error"
    assert data["field"] == "email"


def test_page_forbidden_returns_html():
    """ForbiddenError on page route should return HTML."""
    app = _build_test_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/page/trigger-forbidden")
    assert resp.status_code == 403
    assert "text/html" in resp.headers["content-type"]
    assert resp.headers["X-Error-Code"] == "forbidden"


def test_page_gone_returns_html():
    """GoneError on page route should return HTML."""
    app = _build_test_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/page/trigger-gone")
    assert resp.status_code == 410
    assert "text/html" in resp.headers["content-type"]
    assert resp.headers["X-Error-Code"] == "gone"


# ── X-Error-Code on JSON responses ───────────────────────────────────────────


def test_email_not_verified_header_keeps_casing():
    """The EMAIL_NOT_VERIFIED slug is upper-case in the body — the header
    must carry it verbatim, not a normalised variant."""
    app = _build_test_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/api/v1/trigger-email-not-verified")
    assert resp.status_code == 403
    data = resp.json()
    assert data["code"] == "EMAIL_NOT_VERIFIED"
    assert resp.headers["X-Error-Code"] == "EMAIL_NOT_VERIFIED"


def test_http_exception_fallback_slug_in_header():
    """Unmapped HTTPException statuses fall back to http_<status> — header
    and body must agree."""
    app = _build_test_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/api/v1/trigger-teapot")
    assert resp.status_code == 418
    data = resp.json()
    assert data["code"] == "http_418"
    assert resp.headers["X-Error-Code"] == "http_418"


# ── Edge-composed errors (EDGE_COMPOSED_ERRORS) ──────────────────────────────


def test_redirect_intercept_set_is_subset_of_app_level_set():
    """An origin-empty status the edge doesn't compose is a blank page —
    the hot path may only empty-body what the app-level set covers."""
    from app.middleware.error_handler import (
        EDGE_INTERCEPTED_STATUSES,
        REDIRECT_EDGE_INTERCEPTED_STATUSES,
    )

    assert REDIRECT_EDGE_INTERCEPTED_STATUSES <= EDGE_INTERCEPTED_STATUSES


@pytest.mark.parametrize(
    ("path", "status", "slug"),
    [
        ("/page/trigger-not-found", 404, "not_found"),
        ("/page/trigger-gone", 410, "gone"),
        ("/page/trigger-rate-limited", 429, "rate_limit_exceeded"),
        ("/page/trigger-blocked", 451, "blocked"),
        ("/page/trigger-unhandled", 500, "internal_error"),
    ],
)
def test_edge_composed_intercepted_status_returns_empty_body(
    edge_composed_errors, path, status, slug
):
    """Flag on: intercepted statuses return no body — Caddy composes the page.

    Covers the full EDGE_INTERCEPTED_STATUSES set so an accidental change to
    the set fails here.
    """
    app = _build_test_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get(path)
    assert resp.status_code == status
    assert resp.headers["X-Error-Code"] == slug
    assert resp.content == b""


def test_edge_composed_non_intercepted_status_keeps_body(edge_composed_errors):
    """403 is not in the intercept set — the branded page still renders."""
    app = _build_test_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/page/trigger-forbidden")
    assert resp.status_code == 403
    assert resp.headers["X-Error-Code"] == "forbidden"
    assert "text/html" in resp.headers["content-type"]
    assert resp.content


def test_edge_composed_post_keeps_body(edge_composed_errors):
    """Flag on: non-GET/HEAD methods keep the rendered body (edge only
    intercepts GET/HEAD)."""
    app = _build_test_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.post("/page/trigger-not-found-post")
    assert resp.status_code == 404
    assert resp.headers["X-Error-Code"] == "not_found"
    assert "text/html" in resp.headers["content-type"]
    assert resp.content


def test_edge_composed_json_path_unaffected(edge_composed_errors):
    """Flag on: JSON error responses are byte-identical to flag-off."""
    app = _build_test_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/api/v1/trigger-not-found")
    assert resp.status_code == 404
    data = resp.json()
    assert data["error"] == "resource not found"
    assert data["code"] == "not_found"
    assert resp.headers["X-Error-Code"] == "not_found"
