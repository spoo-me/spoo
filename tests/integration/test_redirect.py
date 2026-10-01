"""
Integration tests for redirect routes — the hot path.

GET  /{short_code}          -> resolve + redirect (302)
POST /{short_code}/password -> password form submission

All DB / Redis / external-service calls are eliminated via
dependency_overrides and a mock lifespan — no real infrastructure needed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from bson import ObjectId
from fastapi.testclient import TestClient

from app.dependencies import get_click_sink, get_url_service
from app.errors import (
    BlockedUrlError,
    ExpiredRedirectError,
    ForbiddenError,
    GoneError,
    NotFoundError,
    ValidationError,
)
from app.routes.redirect_routes import router as redirect_router
from tests.conftest import build_test_app
from tests.factories import make_url_cache


def _make_cache_data(**overrides):
    overrides.setdefault("alias", "abc123")
    overrides.setdefault("long_url", "https://example.com/destination")
    overrides.setdefault("owner_id", str(ObjectId()))
    return make_url_cache(domain="", **overrides)


# ── Tests ────────────────────────────────────────────────────────────────────


def test_redirect_v2_active_url():
    """GET /{code} for an active v2 URL -> 302 with Location header."""
    url_data = _make_cache_data(schema_version="v2")
    mock_url_svc = AsyncMock()
    mock_url_svc.resolve = AsyncMock(return_value=(url_data, "v2"))
    mock_click_sink = AsyncMock()

    app = build_test_app(
        redirect_router,
        overrides={
            get_url_service: lambda: mock_url_svc,
            get_click_sink: lambda: mock_click_sink,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/abc123", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["Location"] == url_data.long_url


def test_redirect_captures_utm_params_on_click_event():
    """utm_* query params on the short link ride the emitted ClickEvent
    (sanitised at event construction)."""
    url_data = _make_cache_data(schema_version="v2")
    mock_url_svc = AsyncMock()
    mock_url_svc.resolve = AsyncMock(return_value=(url_data, "v2"))
    mock_click_sink = AsyncMock()

    app = build_test_app(
        redirect_router,
        overrides={
            get_url_service: lambda: mock_url_svc,
            get_click_sink: lambda: mock_click_sink,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get(
        "/abc123?utm_source=newsletter&utm_medium=email&utm_campaign=%20launch%20",
        follow_redirects=False,
    )

    assert resp.status_code == 302
    event = mock_click_sink.emit.call_args[0][0]
    assert event.utm_source == "newsletter"
    assert event.utm_medium == "email"
    assert event.utm_campaign == "launch"  # whitespace stripped


def test_redirect_without_utm_params_emits_none():
    url_data = _make_cache_data(schema_version="v2")
    mock_url_svc = AsyncMock()
    mock_url_svc.resolve = AsyncMock(return_value=(url_data, "v2"))
    mock_click_sink = AsyncMock()

    app = build_test_app(
        redirect_router,
        overrides={
            get_url_service: lambda: mock_url_svc,
            get_click_sink: lambda: mock_click_sink,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/abc123", follow_redirects=False)

    assert resp.status_code == 302
    event = mock_click_sink.emit.call_args[0][0]
    assert event.utm_source is None
    assert event.utm_medium is None
    assert event.utm_campaign is None


def test_redirect_v1_active_url():
    """GET /{code} for an active v1 URL -> 302."""
    url_data = _make_cache_data(schema_version="v1", alias="xYz789")
    mock_url_svc = AsyncMock()
    mock_url_svc.resolve = AsyncMock(return_value=(url_data, "v1"))
    mock_click_sink = AsyncMock()

    app = build_test_app(
        redirect_router,
        overrides={
            get_url_service: lambda: mock_url_svc,
            get_click_sink: lambda: mock_click_sink,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/xYz789", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["Location"] == url_data.long_url


def test_redirect_emoji_url():
    """GET /{code} for an emoji URL -> 302."""
    url_data = _make_cache_data(schema_version="emoji", alias="\U0001f600\U0001f680")
    mock_url_svc = AsyncMock()
    mock_url_svc.resolve = AsyncMock(return_value=(url_data, "emoji"))
    mock_click_sink = AsyncMock()

    app = build_test_app(
        redirect_router,
        overrides={
            get_url_service: lambda: mock_url_svc,
            get_click_sink: lambda: mock_click_sink,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/%F0%9F%98%80%F0%9F%9A%80", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["Location"] == url_data.long_url


def test_redirect_password_protected_no_password():
    """GET /{code} with password_hash set but no password param -> 401 HTML."""
    url_data = _make_cache_data(password_hash="$2b$12$somebcrypthash")
    mock_url_svc = AsyncMock()
    mock_url_svc.resolve = AsyncMock(return_value=(url_data, "v2"))
    mock_click_sink = AsyncMock()

    app = build_test_app(
        redirect_router,
        overrides={
            get_url_service: lambda: mock_url_svc,
            get_click_sink: lambda: mock_click_sink,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/abc123", follow_redirects=False)

    assert resp.status_code == 401
    assert "text/html" in resp.headers.get("content-type", "")


def test_redirect_v2_correct_password_bcrypt():
    """GET /{code}?password=correct with bcrypt hash -> 302 (verify_password mocked)."""
    url_data = _make_cache_data(password_hash="$2b$12$somebcrypthash")
    mock_url_svc = AsyncMock()
    mock_url_svc.resolve = AsyncMock(return_value=(url_data, "v2"))
    mock_click_sink = AsyncMock()

    app = build_test_app(
        redirect_router,
        overrides={
            get_url_service: lambda: mock_url_svc,
            get_click_sink: lambda: mock_click_sink,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)

    with patch(
        "app.infrastructure.cache.url_cache.verify_password_hash", return_value=True
    ):
        resp = client.get("/abc123?password=correct", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["Location"] == url_data.long_url


def test_redirect_v1_correct_plaintext_password():
    """GET /{code}?password=secret for v1 plaintext password match -> 302."""
    url_data = _make_cache_data(password_hash="secret", schema_version="v1")
    mock_url_svc = AsyncMock()
    mock_url_svc.resolve = AsyncMock(return_value=(url_data, "v1"))
    mock_click_sink = AsyncMock()

    app = build_test_app(
        redirect_router,
        overrides={
            get_url_service: lambda: mock_url_svc,
            get_click_sink: lambda: mock_click_sink,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/abc123?password=secret", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["Location"] == url_data.long_url


def test_redirect_wrong_password():
    """GET /{code}?password=wrong -> 401 HTML password page."""
    url_data = _make_cache_data(password_hash="$2b$12$somebcrypthash")
    mock_url_svc = AsyncMock()
    mock_url_svc.resolve = AsyncMock(return_value=(url_data, "v2"))
    mock_click_sink = AsyncMock()

    app = build_test_app(
        redirect_router,
        overrides={
            get_url_service: lambda: mock_url_svc,
            get_click_sink: lambda: mock_click_sink,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)

    with patch(
        "app.infrastructure.cache.url_cache.verify_password_hash", return_value=False
    ):
        resp = client.get("/abc123?password=wrong", follow_redirects=False)

    assert resp.status_code == 401
    assert "text/html" in resp.headers.get("content-type", "")


def test_redirect_blocked_url():
    """resolve raises BlockedUrlError -> 451 HTML."""
    mock_url_svc = AsyncMock()
    mock_url_svc.resolve = AsyncMock(side_effect=BlockedUrlError("Blocked"))
    mock_click_sink = AsyncMock()

    app = build_test_app(
        redirect_router,
        overrides={
            get_url_service: lambda: mock_url_svc,
            get_click_sink: lambda: mock_click_sink,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/abc123", follow_redirects=False)

    assert resp.status_code == 451
    assert "text/html" in resp.headers.get("content-type", "")


def test_redirect_expired_url():
    """resolve raises GoneError -> 410 HTML."""
    mock_url_svc = AsyncMock()
    mock_url_svc.resolve = AsyncMock(side_effect=GoneError("Expired"))
    mock_click_sink = AsyncMock()

    app = build_test_app(
        redirect_router,
        overrides={
            get_url_service: lambda: mock_url_svc,
            get_click_sink: lambda: mock_click_sink,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/abc123", follow_redirects=False)

    assert resp.status_code == 410
    assert "text/html" in resp.headers.get("content-type", "")


def test_redirect_expired_with_fallback_302s_and_records_no_click():
    """resolve raises ExpiredRedirectError -> 302 to the owner's fallback,
    uncacheable, and the click sink never hears about it."""
    mock_url_svc = AsyncMock()
    mock_url_svc.resolve = AsyncMock(
        side_effect=ExpiredRedirectError("https://fallback.example/ended")
    )
    mock_click_sink = AsyncMock()

    app = build_test_app(
        redirect_router,
        overrides={
            get_url_service: lambda: mock_url_svc,
            get_click_sink: lambda: mock_click_sink,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/abc123", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["Location"] == "https://fallback.example/ended"
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["X-Robots-Tag"] == "noindex, nofollow, noarchive"
    mock_click_sink.emit.assert_not_awaited()


def test_redirect_inactive_url():
    """resolve raises GoneError for inactive URL -> 410 HTML."""
    mock_url_svc = AsyncMock()
    mock_url_svc.resolve = AsyncMock(side_effect=GoneError("Inactive"))
    mock_click_sink = AsyncMock()

    app = build_test_app(
        redirect_router,
        overrides={
            get_url_service: lambda: mock_url_svc,
            get_click_sink: lambda: mock_click_sink,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/someCode", follow_redirects=False)

    assert resp.status_code == 410
    assert "text/html" in resp.headers.get("content-type", "")


def test_redirect_not_found():
    """resolve raises NotFoundError -> 404 HTML."""
    mock_url_svc = AsyncMock()
    mock_url_svc.resolve = AsyncMock(side_effect=NotFoundError("Not found"))
    mock_click_sink = AsyncMock()

    app = build_test_app(
        redirect_router,
        overrides={
            get_url_service: lambda: mock_url_svc,
            get_click_sink: lambda: mock_click_sink,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/nope42", follow_redirects=False)

    assert resp.status_code == 404
    assert "text/html" in resp.headers.get("content-type", "")


def test_redirect_max_clicks_reached():
    """After max clicks, resolve raises GoneError -> 410."""
    mock_url_svc = AsyncMock()
    mock_url_svc.resolve = AsyncMock(side_effect=GoneError("Max clicks reached"))
    mock_click_sink = AsyncMock()

    app = build_test_app(
        redirect_router,
        overrides={
            get_url_service: lambda: mock_url_svc,
            get_click_sink: lambda: mock_click_sink,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/abc123", follow_redirects=False)

    assert resp.status_code == 410


def test_redirect_bot_blocked_v1():
    """the click sink raises ForbiddenError for bot on v1 -> 403 JSON."""
    url_data = _make_cache_data(schema_version="v1", block_bots=True)
    mock_url_svc = AsyncMock()
    mock_url_svc.resolve = AsyncMock(return_value=(url_data, "v1"))
    mock_click_sink = AsyncMock()
    mock_click_sink.emit = AsyncMock(side_effect=ForbiddenError("Bot access denied"))

    app = build_test_app(
        redirect_router,
        overrides={
            get_url_service: lambda: mock_url_svc,
            get_click_sink: lambda: mock_click_sink,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/abc123", follow_redirects=False)

    assert resp.status_code == 403
    assert "text/html" in resp.headers["content-type"]


def test_redirect_bot_blocked_v2():
    """v2 with block_bots, the click sink raises ForbiddenError -> 403 HTML."""
    url_data = _make_cache_data(schema_version="v2", block_bots=True)
    mock_url_svc = AsyncMock()
    mock_url_svc.resolve = AsyncMock(return_value=(url_data, "v2"))
    mock_click_sink = AsyncMock()
    mock_click_sink.emit = AsyncMock(side_effect=ForbiddenError("Bot access denied"))

    app = build_test_app(
        redirect_router,
        overrides={
            get_url_service: lambda: mock_url_svc,
            get_click_sink: lambda: mock_click_sink,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/abc123", follow_redirects=False)

    assert resp.status_code == 403
    assert "text/html" in resp.headers["content-type"]


def test_redirect_bad_user_agent_still_redirects():
    """the click sink raises ValidationError -> skip analytics, still 302."""
    url_data = _make_cache_data()
    mock_url_svc = AsyncMock()
    mock_url_svc.resolve = AsyncMock(return_value=(url_data, "v2"))
    mock_click_sink = AsyncMock()
    mock_click_sink.emit = AsyncMock(side_effect=ValidationError("Bad User-Agent"))

    app = build_test_app(
        redirect_router,
        overrides={
            get_url_service: lambda: mock_url_svc,
            get_click_sink: lambda: mock_click_sink,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/abc123", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["Location"] == url_data.long_url


def test_redirect_head_request_skips_tracking():
    """HEAD /{code} -> 302, track_click NOT called."""
    url_data = _make_cache_data()
    mock_url_svc = AsyncMock()
    mock_url_svc.resolve = AsyncMock(return_value=(url_data, "v2"))
    mock_click_sink = AsyncMock()

    app = build_test_app(
        redirect_router,
        overrides={
            get_url_service: lambda: mock_url_svc,
            get_click_sink: lambda: mock_click_sink,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.head("/abc123", follow_redirects=False)

    assert resp.status_code == 302
    mock_click_sink.emit.assert_not_called()


def test_redirect_sets_x_robots_tag():
    """Response has X-Robots-Tag: noindex, nofollow."""
    url_data = _make_cache_data()
    mock_url_svc = AsyncMock()
    mock_url_svc.resolve = AsyncMock(return_value=(url_data, "v2"))
    mock_click_sink = AsyncMock()

    app = build_test_app(
        redirect_router,
        overrides={
            get_url_service: lambda: mock_url_svc,
            get_click_sink: lambda: mock_click_sink,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/abc123", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers.get("X-Robots-Tag") == "noindex, nofollow, noarchive"


def test_password_form_submit_correct():
    """POST /{code}/password with correct password -> 302 redirect with password param."""
    url_data = _make_cache_data(password_hash="$2b$12$somebcrypthash")
    mock_url_svc = AsyncMock()
    mock_url_svc.resolve = AsyncMock(return_value=(url_data, "v2"))

    app = build_test_app(
        redirect_router,
        overrides={
            get_url_service: lambda: mock_url_svc,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)

    with patch(
        "app.infrastructure.cache.url_cache.verify_password_hash", return_value=True
    ):
        resp = client.post(
            "/abc123/password",
            data={"password": "correct"},
            follow_redirects=False,
        )

    assert resp.status_code == 302
    assert "/abc123?password=correct" in resp.headers["Location"]


def test_password_form_submit_wrong():
    """POST /{code}/password with wrong password -> 200 HTML (re-render)."""
    url_data = _make_cache_data(password_hash="$2b$12$somebcrypthash")
    mock_url_svc = AsyncMock()
    mock_url_svc.resolve = AsyncMock(return_value=(url_data, "v2"))

    app = build_test_app(
        redirect_router,
        overrides={
            get_url_service: lambda: mock_url_svc,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)

    with patch(
        "app.infrastructure.cache.url_cache.verify_password_hash", return_value=False
    ):
        resp = client.post(
            "/abc123/password",
            data={"password": "wrong"},
            follow_redirects=False,
        )

    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


def test_password_form_url_not_found():
    """POST /{code}/password for missing URL -> 400 HTML."""
    mock_url_svc = AsyncMock()
    mock_url_svc.resolve = AsyncMock(side_effect=NotFoundError("Not found"))

    app = build_test_app(
        redirect_router,
        overrides={
            get_url_service: lambda: mock_url_svc,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/abc123/password",
        data={"password": "anything"},
        follow_redirects=False,
    )

    assert resp.status_code == 400
    assert "text/html" in resp.headers.get("content-type", "")


def test_password_form_not_password_protected():
    """POST /{code}/password for non-protected URL -> 400 HTML."""
    url_data = _make_cache_data(password_hash=None)
    mock_url_svc = AsyncMock()
    mock_url_svc.resolve = AsyncMock(return_value=(url_data, "v2"))

    app = build_test_app(
        redirect_router,
        overrides={
            get_url_service: lambda: mock_url_svc,
        },
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/abc123/password",
        data={"password": "anything"},
        follow_redirects=False,
    )

    assert resp.status_code == 400
    assert "text/html" in resp.headers.get("content-type", "")
