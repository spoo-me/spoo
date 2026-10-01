"""
Integration tests for click tracking behaviour on the redirect route.

Tests verify that a ClickEvent is emitted through the sink with correct
fields on GET, skipped on HEAD, and that error scenarios (bad UA, bot
block, unexpected crash) are handled gracefully.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from app.dependencies import get_click_sink, get_url_service
from app.errors import ForbiddenError, ValidationError
from app.infrastructure.cache.url_cache import UrlCacheData
from app.routes.redirect_routes import router as redirect_router
from app.services.click.events import ClickEvent
from tests.conftest import build_test_app
from tests.factories import make_url_cache


def _make_url_cache(schema: str = "v2", **overrides) -> UrlCacheData:
    # Shape lives in tests/factories.py; this file's tests spell the
    # schema kwarg without the _version suffix.
    return make_url_cache(schema_version=schema, domain="", **overrides)


def _mock_url_service(url_data: UrlCacheData, schema: str = "v2") -> MagicMock:
    svc = MagicMock()
    svc.resolve = AsyncMock(return_value=(url_data, schema))
    return svc


def _mock_click_sink() -> MagicMock:
    sink = MagicMock()
    sink.emit = AsyncMock(return_value=None)
    return sink


def _build_app(url_svc, click_sink) -> object:
    return build_test_app(
        redirect_router,
        overrides={
            get_url_service: lambda: url_svc,
            get_click_sink: lambda: click_sink,
        },
    )


def _emitted_event(sink: MagicMock) -> ClickEvent:
    sink.emit.assert_called_once()
    return sink.emit.call_args.args[0]


# ── Click tracking on GET ────────────────────────────────────────────────────


def test_click_tracked_on_redirect():
    """GET /{code} should emit a click event and still redirect."""
    url_data = _make_url_cache(long_url="https://example.com/target")
    sink = _mock_click_sink()
    app = _build_app(_mock_url_service(url_data), sink)
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as c:
        resp = c.get("/abc1234", headers={"User-Agent": "Mozilla/5.0"})
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://example.com/target"
    sink.emit.assert_called_once()


def test_click_not_tracked_on_head():
    """HEAD /{code} should NOT emit."""
    url_data = _make_url_cache(long_url="https://example.com")
    sink = _mock_click_sink()
    app = _build_app(_mock_url_service(url_data), sink)
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as c:
        resp = c.head("/abc1234")
    assert resp.status_code == 302
    sink.emit.assert_not_called()


def test_click_not_tracked_on_password_page():
    """GET /{code} for a password-protected URL (no password) should NOT emit."""
    url_data = _make_url_cache(password_hash="$2b$12$hashed")
    sink = _mock_click_sink()
    app = _build_app(_mock_url_service(url_data), sink)
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/abc1234")
    assert resp.status_code == 401  # password page
    sink.emit.assert_not_called()


def test_click_bad_user_agent_skips_but_redirects():
    """ValidationError from the sink (bad UA) skips analytics but still redirects."""
    url_data = _make_url_cache(long_url="https://example.com")
    sink = MagicMock()
    sink.emit = AsyncMock(side_effect=ValidationError("bad UA"))
    app = _build_app(_mock_url_service(url_data), sink)
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as c:
        resp = c.get("/abc1234", headers={"User-Agent": ""})
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://example.com"


def test_click_bot_blocked_returns_403():
    """ForbiddenError from the sink (inline v1 bot block) → 403, no redirect."""
    url_data = _make_url_cache(
        schema="v1", long_url="https://example.com", block_bots=True
    )
    sink = MagicMock()
    sink.emit = AsyncMock(side_effect=ForbiddenError("bots not allowed on this URL"))
    app = _build_app(_mock_url_service(url_data, schema="v1"), sink)
    with TestClient(app, raise_server_exceptions=False) as c:
        # Browser-like UA so the route-level pre-check doesn't trip first;
        # this exercises the defense-in-depth path through the sink.
        resp = c.get("/abc123", headers={"User-Agent": "Mozilla/5.0 (Test)"})
    assert resp.status_code == 403
    assert "text/html" in resp.headers["content-type"]


def test_click_event_carries_correct_fields():
    """The emitted event snapshots url_data, short_code, schema, and headers."""
    url_data = _make_url_cache(alias="mycode", long_url="https://example.com")
    sink = _mock_click_sink()
    app = _build_app(_mock_url_service(url_data, schema="v2"), sink)
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as c:
        c.get(
            "/mycode",
            headers={
                "User-Agent": "Mozilla/5.0 (Test)",
                "Referer": "https://referrer.com",
            },
        )
    event = _emitted_event(sink)
    assert event.url.alias == "mycode"
    assert event.url.long_url == "https://example.com"
    assert event.short_code == "mycode"
    assert event.schema_key == "v2"
    assert event.is_emoji is False
    assert event.user_agent == "Mozilla/5.0 (Test)"
    assert event.referrer == "https://referrer.com"
    assert event.redirect_ms >= 0


def test_click_error_does_not_crash_redirect():
    """Unexpected exception from the sink is swallowed — still redirects."""
    url_data = _make_url_cache(long_url="https://example.com/safe")
    sink = MagicMock()
    sink.emit = AsyncMock(side_effect=RuntimeError("DB connection lost"))
    app = _build_app(_mock_url_service(url_data), sink)
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as c:
        resp = c.get("/abc1234", headers={"User-Agent": "Mozilla/5.0"})
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://example.com/safe"


def test_click_tracked_with_user_agent_header():
    """The User-Agent header value should land on the event."""
    url_data = _make_url_cache(long_url="https://example.com")
    sink = _mock_click_sink()
    app = _build_app(_mock_url_service(url_data), sink)
    custom_ua = "CustomBrowser/1.0 (Linux; x86_64)"
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as c:
        c.get("/abc1234", headers={"User-Agent": custom_ua})
    assert _emitted_event(sink).user_agent == custom_ua


def test_click_tracked_with_referer_header():
    """The Referer header value should land on the event."""
    url_data = _make_url_cache(long_url="https://example.com")
    sink = _mock_click_sink()
    app = _build_app(_mock_url_service(url_data), sink)
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as c:
        c.get("/abc1234", headers={"Referer": "https://twitter.com/status/123"})
    assert _emitted_event(sink).referrer == "https://twitter.com/status/123"


def test_click_tracked_with_none_referer_when_absent():
    """When no Referer header is present, the event carries referrer=None."""
    url_data = _make_url_cache(long_url="https://example.com")
    sink = _mock_click_sink()
    app = _build_app(_mock_url_service(url_data), sink)
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as c:
        c.get("/abc1234", headers={"User-Agent": "Mozilla/5.0"})
    assert _emitted_event(sink).referrer is None


def test_click_emoji_schema_sets_is_emoji_true():
    """When schema is 'emoji', the event carries is_emoji=True."""
    url_data = _make_url_cache(
        alias="smile123", schema="emoji", long_url="https://example.com"
    )
    sink = _mock_click_sink()
    app = _build_app(_mock_url_service(url_data, schema="emoji"), sink)
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as c:
        c.get("/smile123", headers={"User-Agent": "Mozilla/5.0"})
    event = _emitted_event(sink)
    assert event.is_emoji is True
    assert event.schema_key == "emoji"


def test_redirect_sets_noindex_nofollow_header():
    """Redirect response should include X-Robots-Tag: noindex, nofollow, noarchive."""
    url_data = _make_url_cache(long_url="https://example.com")
    sink = _mock_click_sink()
    app = _build_app(_mock_url_service(url_data), sink)
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as c:
        resp = c.get("/abc1234")
    assert resp.headers.get("x-robots-tag") == "noindex, nofollow, noarchive"
