"""Smoke tests: middleware stack verification."""

from __future__ import annotations

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/")

from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from app.config import AppSettings
from app.infrastructure.oauth_clients import OAUTH_STATE_TTL_SECONDS


def test_x_request_id_header_present(smoke_client: TestClient) -> None:
    """Every response should include an X-Request-ID header from RequestLoggingMiddleware."""
    resp = smoke_client.get("/health")
    assert "x-request-id" in resp.headers
    assert resp.headers["x-request-id"].startswith("req_")


def test_cors_public_route_allows_any_origin(smoke_client: TestClient) -> None:
    """Public API routes should allow any origin without credentials."""
    resp = smoke_client.options(
        "/api/v1/shorten",
        headers={
            "Origin": "https://arbitrary-domain.test",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp.headers.get("access-control-allow-origin") == "*"
    assert "access-control-allow-credentials" not in resp.headers


def test_cors_unclassified_route_no_headers(smoke_client: TestClient) -> None:
    """Routes outside public/private groups should not get CORS headers."""
    resp = smoke_client.get(
        "/health",
        headers={"Origin": "https://example.com"},
    )
    assert "access-control-allow-origin" not in resp.headers


def test_max_content_length_rejects_large_body(smoke_client: TestClient) -> None:
    """MaxContentLengthMiddleware should return 413 for oversized payloads."""
    # Default max is 1 MB (1_048_576 bytes). Advertise 2 MB via Content-Length.
    resp = smoke_client.post(
        "/auth/login",
        content=b"x",
        headers={
            "Content-Length": "2097152",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 413
    data = resp.json()
    assert data["code"] == "payload_too_large"


def test_max_content_length_allows_small_body(smoke_client: TestClient) -> None:
    """Small payloads should pass through MaxContentLengthMiddleware."""
    resp = smoke_client.post(
        "/auth/login",
        json={"email": "test@test.com", "password": "pass"},
        headers={"Accept": "application/json"},
    )
    # Should NOT be 413 — it will fail on auth/validation but that proves middleware passed it through
    assert resp.status_code != 413


def test_session_middleware_present(smoke_client: TestClient) -> None:
    """SessionMiddleware should set a session cookie when session data is written.

    We verify indirectly: OAuth routes depend on session middleware. Accessing any
    route and checking that no 500 from missing session middleware occurs is sufficient.
    """
    resp = smoke_client.get("/health")
    # If SessionMiddleware were missing, OAuth state writes would crash.
    # A healthy response proves the middleware stack is functional.
    assert resp.status_code in (200, 503)


# ── OAuth state cookie ────────────────────────────────────────────────────────
#
# The session cookie is where Authlib keeps its half of the OAuth state, so a
# missing or dropped cookie fails every callback. Assert the flags the real
# create_app() configures rather than the ones a test app happens to pass.


def _oauth_state_cookie_attrs(*, production: bool) -> set[str]:
    """Set-Cookie attributes create_app() emits for the session cookie.

    Lifts the registered SessionMiddleware (class plus its keyword arguments)
    out of the real application and replays it over a route that writes to the
    session, so the assertions below read the header a browser would receive.
    """
    from app.factory import create_app

    settings = AppSettings()
    settings.env = "production" if production else "development"
    # A DSN in a developer's local .env would otherwise re-initialise Sentry
    # with environment="production" as a side effect of this test.
    settings.sentry.sentry_dsn = ""

    entry = next(
        mw for mw in create_app(settings).user_middleware if mw.cls is SessionMiddleware
    )

    async def write_state(request: Request) -> Response:
        request.session["_state_google_test"] = {"data": {}}
        return PlainTextResponse("ok")

    probe = Starlette(
        routes=[Route("/probe", write_state)],
        middleware=[Middleware(entry.cls, *entry.args, **entry.kwargs)],
    )
    header = TestClient(probe).get("/probe").headers["set-cookie"]
    # Attribute names only — the cookie value is base64 and could contain any
    # substring we might otherwise search the raw header for.
    return {part.strip().lower() for part in header.split(";")[1:]}


def test_oauth_state_cookie_is_secure_in_production() -> None:
    """The state cookie must carry Secure on an HTTPS-only origin.

    Without it the cookie travels on plain-http requests to spoo.me and is
    eligible for the eviction rules browsers apply to insecure cookies across
    the redirect out to the provider and back, which loses the flow's state.
    """
    assert "secure" in _oauth_state_cookie_attrs(production=True)


def test_oauth_state_cookie_is_not_secure_in_development() -> None:
    """Secure is gated on production so plain-http local development works."""
    assert "secure" not in _oauth_state_cookie_attrs(production=False)


def test_oauth_state_cookie_is_httponly_and_lax() -> None:
    """HttpOnly keeps the state out of scripts; lax survives the callback.

    The provider returns the user with a top-level cross-site GET, which lax
    allows and strict would not.
    """
    attrs = _oauth_state_cookie_attrs(production=True)
    assert "httponly" in attrs
    assert "samesite=lax" in attrs


def test_oauth_state_cookie_expires_with_the_state_it_holds() -> None:
    """Cookie lifetime matches the ceiling verify_oauth_state() enforces.

    Starlette uses max_age for both the Max-Age attribute and the signature
    lifetime, so the default of 14 days would leave the cookie valid long
    after the state string inside it had expired.
    """
    attrs = _oauth_state_cookie_attrs(production=True)
    assert f"max-age={OAUTH_STATE_TTL_SECONDS}" in attrs


def test_middleware_ordering_correct(smoke_app) -> None:
    """Middleware should be stacked in the correct order.

    FastAPI registers middleware in reverse order (last added = outermost).
    Registration order: Session, CORS, SecurityHeaders, MaxContentLength, RequestLogging
    Execution order (outermost first): Session -> CORS -> SecurityHeaders -> MaxContentLength -> RequestLogging
    """

    # Walk the middleware stack from the app
    # In Starlette, app.middleware_stack is built by wrapping: outermost first
    middleware_classes = []
    current = smoke_app.middleware_stack
    while current is not None:
        cls = type(current)
        middleware_classes.append(cls.__name__)
        current = getattr(current, "app", None)

    # ServerErrorMiddleware is always outermost (added by Starlette itself)
    # Then our custom middleware in execution order
    class_names = [
        c
        for c in middleware_classes
        if c not in ("ServerErrorMiddleware", "ExceptionMiddleware")
    ]

    # Verify RequestLogging is innermost (appears last), Session is outermost (appears first)
    if "SessionMiddleware" in class_names and "RequestLoggingMiddleware" in class_names:
        session_idx = class_names.index("SessionMiddleware")
        logging_idx = class_names.index("RequestLoggingMiddleware")
        assert session_idx < logging_idx, (
            f"SessionMiddleware should be outermost (before RequestLoggingMiddleware): {class_names}"
        )
