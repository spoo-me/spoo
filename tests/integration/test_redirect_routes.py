"""
Integration tests for the redirect and legacy URL shortener routes.

Uses the _build_test_app pattern from test_api_v1.py — no real infrastructure needed.
The redirect route emits ClickEvents through a ClickEventSink; these tests override
``get_click_sink`` and assert on the emitted event.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_click_sink, get_url_service
from app.errors import (
    BlockedUrlError,
    ForbiddenError,
    GoneError,
    NotFoundError,
    NotYetLiveError,
    ValidationError,
)
from app.infrastructure.cache.url_cache import UrlCacheData
from app.routes.redirect_routes import router as redirect_router
from app.schemas.models.url import AbVariant
from app.services.click.events import ClickEvent
from tests.conftest import build_test_app
from tests.factories import make_url_cache


def _make_url_cache(schema: str = "v2", **overrides) -> UrlCacheData:
    # Shape lives in tests/factories.py; this file's tests spell the
    # schema kwarg without the _version suffix.
    return make_url_cache(schema_version=schema, domain="", **overrides)


BOT_UA = "Googlebot/2.1 (+http://www.google.com/bot.html)"
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


# ── Mock services ─────────────────────────────────────────────────────────────


def _mock_url_service(url_data: UrlCacheData, schema: str = "v2"):
    svc = MagicMock()
    svc.resolve = AsyncMock(return_value=(url_data, schema))
    return svc


def _mock_click_sink():
    sink = MagicMock()
    sink.emit = AsyncMock(return_value=None)
    return sink


def _build_app(url_svc, click_sink=None):
    return build_test_app(
        redirect_router,
        overrides={
            get_url_service: lambda: url_svc,
            get_click_sink: lambda: click_sink or _mock_click_sink(),
        },
    )


# ── Redirect tests ────────────────────────────────────────────────────────────


def test_redirect_v2_url():
    url_data = _make_url_cache(long_url="https://example.com/target")
    app = _build_app(_mock_url_service(url_data))
    with TestClient(app, follow_redirects=False) as client:
        resp = client.get("/abc1234")
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://example.com/target"
    assert resp.headers.get("x-robots-tag") == "noindex, nofollow, noarchive"


def test_redirect_emits_click_event_snapshot():
    """The emitted event snapshots the resolved URL so consumers never re-resolve."""
    url_data = _make_url_cache(long_url="https://example.com/target")
    sink = _mock_click_sink()
    app = _build_app(_mock_url_service(url_data), sink)
    with TestClient(app, follow_redirects=False) as client:
        resp = client.get(
            "/abc1234",
            headers={"User-Agent": BROWSER_UA, "Referer": "https://t.co/x"},
        )
    assert resp.status_code == 302
    sink.emit.assert_awaited_once()
    event = sink.emit.await_args.args[0]
    assert isinstance(event, ClickEvent)
    assert event.short_code == "abc1234"
    assert event.schema_key == "v2"
    assert event.is_emoji is False
    assert event.url.long_url == "https://example.com/target"
    assert event.user_agent == BROWSER_UA
    assert event.referrer == "https://t.co/x"
    assert event.redirect_ms >= 0


def test_redirect_event_carries_resolved_alias_not_path_form():
    """Emoji codes can resolve through a byte-variant (VS16) of the stored
    alias; the event must carry the RESOLVED identity — click handlers key
    legacy _id updates and v2 max-clicks cache invalidation off it, and the
    raw path form would silently drop those writes."""
    url_data = _make_url_cache(alias="⭐🎉", long_url="https://example.com/star")
    sink = _mock_click_sink()
    app = _build_app(_mock_url_service(url_data), sink)
    with TestClient(app, follow_redirects=False) as client:
        resp = client.get("/⭐️🎉", headers={"User-Agent": BROWSER_UA})  # VS16 variant
    assert resp.status_code == 302
    event = sink.emit.await_args.args[0]
    assert event.short_code == "⭐🎉"


def test_redirect_event_strips_password_hash():
    """v1 password hashes are plaintext — they must never enter the event."""
    url_data = _make_url_cache(password_hash="mypassword", schema="v1")
    sink = _mock_click_sink()
    app = _build_app(_mock_url_service(url_data, schema="v1"), sink)
    with TestClient(app, follow_redirects=False) as client:
        resp = client.get("/abc1234?password=mypassword")
    assert resp.status_code == 302
    event = sink.emit.await_args.args[0]
    assert event.url.password_hash is None


def test_redirect_not_found_returns_404_html():
    url_svc = MagicMock()
    url_svc.resolve = AsyncMock(side_effect=NotFoundError("not found"))
    app = _build_app(url_svc)
    with TestClient(app) as client:
        resp = client.get("/notexist")
    assert resp.status_code == 404
    assert "text/html" in resp.headers["content-type"]
    assert resp.headers["X-Error-Code"] == "not_found"


def test_redirect_blocked_url_returns_451_html():
    url_svc = MagicMock()
    url_svc.resolve = AsyncMock(side_effect=BlockedUrlError("blocked"))
    app = _build_app(url_svc)
    with TestClient(app) as client:
        resp = client.get("/blocked1")
    assert resp.status_code == 451
    assert "text/html" in resp.headers["content-type"]
    assert resp.headers["X-Error-Code"] == "blocked"


def test_redirect_expired_url_returns_410_html():
    url_svc = MagicMock()
    url_svc.resolve = AsyncMock(side_effect=GoneError("expired"))
    app = _build_app(url_svc)
    with TestClient(app) as client:
        resp = client.get("/expired1")
    assert resp.status_code == 410
    assert "text/html" in resp.headers["content-type"]
    assert resp.headers["X-Error-Code"] == "gone"


def test_redirect_not_found_edge_composed_returns_empty_body(edge_composed_errors):
    """EDGE_COMPOSED_ERRORS on: the hot-path 404 skips the template — Caddy
    discards the body and composes the Next error page from X-Error-Code."""
    url_svc = MagicMock()
    url_svc.resolve = AsyncMock(side_effect=NotFoundError("not found"))
    app = _build_app(url_svc)
    with TestClient(app) as client:
        resp = client.get("/notexist")
    assert resp.status_code == 404
    assert resp.headers["X-Error-Code"] == "not_found"
    assert resp.content == b""


def test_redirect_expired_edge_composed_returns_empty_body(edge_composed_errors):
    """EDGE_COMPOSED_ERRORS on: the hot-path 410 also skips the template."""
    url_svc = MagicMock()
    url_svc.resolve = AsyncMock(side_effect=GoneError("expired"))
    app = _build_app(url_svc)
    with TestClient(app) as client:
        resp = client.get("/expired1")
    assert resp.status_code == 410
    assert resp.headers["X-Error-Code"] == "gone"
    assert resp.content == b""


def test_redirect_blocked_edge_composed_returns_empty_body(edge_composed_errors):
    """EDGE_COMPOSED_ERRORS on: the hot-path 451 also skips the template."""
    url_svc = MagicMock()
    url_svc.resolve = AsyncMock(side_effect=BlockedUrlError("blocked"))
    app = _build_app(url_svc)
    with TestClient(app) as client:
        resp = client.get("/blocked1")
    assert resp.status_code == 451
    assert resp.headers["X-Error-Code"] == "blocked"
    assert resp.content == b""


def test_redirect_bot_block_edge_composed_keeps_body(edge_composed_errors):
    """403 is excluded from the redirect intercept set — the bot-block page
    stays server-rendered while still self-describing via X-Error-Code."""
    url_data = _make_url_cache(schema="v1", block_bots=True)
    app = _build_app(_mock_url_service(url_data, schema="v1"))
    with TestClient(app) as client:
        resp = client.get("/abc123", headers={"User-Agent": BOT_UA})
    assert resp.status_code == 403
    assert resp.headers["X-Error-Code"] == "forbidden"
    assert "text/html" in resp.headers["content-type"]
    assert resp.content


def test_redirect_password_protected_no_password_returns_401_html():
    url_data = _make_url_cache(password_hash="$2b$12$hashed")
    app = _build_app(_mock_url_service(url_data))
    with TestClient(app) as client:
        resp = client.get("/abc1234")
    assert resp.status_code == 401
    assert "text/html" in resp.headers["content-type"]


def test_redirect_v2_wrong_password_returns_401_html():
    url_data = _make_url_cache(password_hash="$2b$12$hashed")
    app = _build_app(_mock_url_service(url_data))
    with TestClient(app, follow_redirects=False) as client:
        resp = client.get("/abc1234?password=wrongpassword")
    assert resp.status_code == 401
    assert "text/html" in resp.headers["content-type"]


def test_redirect_v1_correct_plaintext_password_redirects():
    url_data = _make_url_cache(
        password_hash="mypassword", schema="v1", long_url="https://example.com"
    )
    app = _build_app(_mock_url_service(url_data, schema="v1"))
    with TestClient(app, follow_redirects=False) as client:
        resp = client.get("/abc123?password=mypassword")
    assert resp.status_code == 302


def test_redirect_bad_user_agent_skips_analytics_but_redirects():
    """ValidationError from the sink = bad UA → skip analytics, still redirect."""
    url_data = _make_url_cache(long_url="https://example.com")
    sink = MagicMock()
    sink.emit = AsyncMock(side_effect=ValidationError("bad UA"))
    app = _build_app(_mock_url_service(url_data), sink)
    with TestClient(app, follow_redirects=False) as client:
        resp = client.get("/abc1234", headers={"User-Agent": ""})
    assert resp.status_code == 302


def test_redirect_sink_forbidden_blocks_redirect():
    """Defense in depth: ForbiddenError raised by the inline sink → 403."""
    url_data = _make_url_cache(
        schema="v1", long_url="https://example.com", block_bots=True
    )
    sink = MagicMock()
    sink.emit = AsyncMock(side_effect=ForbiddenError("bots not allowed"))
    app = _build_app(_mock_url_service(url_data, schema="v1"), sink)
    with TestClient(app) as client:
        resp = client.get("/abc123", headers={"User-Agent": BROWSER_UA})
    assert resp.status_code == 403
    assert "text/html" in resp.headers["content-type"]
    assert resp.headers["X-Error-Code"] == "forbidden"


def test_redirect_sink_unexpected_error_still_redirects():
    """A broken click pipeline must never take down the redirect."""
    url_data = _make_url_cache(long_url="https://example.com")
    sink = MagicMock()
    sink.emit = AsyncMock(side_effect=RuntimeError("pipeline exploded"))
    app = _build_app(_mock_url_service(url_data), sink)
    with TestClient(app, follow_redirects=False) as client:
        resp = client.get("/abc1234")
    assert resp.status_code == 302


# ── Pre-emit bot block tests ──────────────────────────────────────────────────


def test_v1_bot_blocked_before_emit():
    """block_bots v1 URL + bot UA → 403 decided in the route, no event emitted."""
    url_data = _make_url_cache(schema="v1", block_bots=True)
    sink = _mock_click_sink()
    app = _build_app(_mock_url_service(url_data, schema="v1"), sink)
    with TestClient(app) as client:
        resp = client.get("/abc123", headers={"User-Agent": BOT_UA})
    assert resp.status_code == 403
    sink.emit.assert_not_awaited()


def test_emoji_bot_blocked_before_emit():
    url_data = _make_url_cache(schema="v1", block_bots=True)
    sink = _mock_click_sink()
    app = _build_app(_mock_url_service(url_data, schema="emoji"), sink)
    with TestClient(app) as client:
        resp = client.get("/%F0%9F%90%8D", headers={"User-Agent": BOT_UA})
    assert resp.status_code == 403
    sink.emit.assert_not_awaited()


def test_v2_bot_not_blocked_at_route():
    """v2 bot handling stays in the pipeline: analytics skipped, not the redirect."""
    url_data = _make_url_cache(schema="v2", block_bots=True)
    sink = _mock_click_sink()
    app = _build_app(_mock_url_service(url_data), sink)
    with TestClient(app, follow_redirects=False) as client:
        resp = client.get("/abc1234", headers={"User-Agent": BOT_UA})
    assert resp.status_code == 302
    sink.emit.assert_awaited_once()


def test_v1_non_bot_not_blocked():
    url_data = _make_url_cache(schema="v1", block_bots=True)
    sink = _mock_click_sink()
    app = _build_app(_mock_url_service(url_data, schema="v1"), sink)
    with TestClient(app, follow_redirects=False) as client:
        resp = client.get("/abc123", headers={"User-Agent": BROWSER_UA})
    assert resp.status_code == 302
    sink.emit.assert_awaited_once()


def test_v1_bot_with_empty_ua_not_pre_blocked():
    """Empty UA can't be classified — matches inline behavior (ValidationError
    path: skip analytics, still redirect)."""
    url_data = _make_url_cache(schema="v1", block_bots=True)
    sink = _mock_click_sink()
    app = _build_app(_mock_url_service(url_data, schema="v1"), sink)
    with TestClient(app, follow_redirects=False) as client:
        resp = client.get("/abc123", headers={"User-Agent": ""})
    assert resp.status_code == 302


def test_redirect_head_skips_click_tracking():
    """HEAD requests skip analytics — nothing is emitted."""
    url_data = _make_url_cache(long_url="https://example.com")
    sink = _mock_click_sink()
    app = _build_app(_mock_url_service(url_data), sink)
    with TestClient(app, follow_redirects=False) as client:
        resp = client.head("/abc1234")
    assert resp.status_code == 302
    sink.emit.assert_not_awaited()


def test_head_bot_on_blocked_v1_still_not_pre_blocked():
    """HEAD is exempt from tracking AND from the pre-emit bot decision."""
    url_data = _make_url_cache(schema="v1", block_bots=True)
    sink = _mock_click_sink()
    app = _build_app(_mock_url_service(url_data, schema="v1"), sink)
    with TestClient(app, follow_redirects=False) as client:
        resp = client.head("/abc123", headers={"User-Agent": BOT_UA})
    assert resp.status_code == 302
    sink.emit.assert_not_awaited()


# ── Password form tests ───────────────────────────────────────────────────────


def test_password_form_correct_password_redirects():
    url_data = _make_url_cache(
        password_hash="mypassword", schema="v1", long_url="https://example.com"
    )
    app = _build_app(_mock_url_service(url_data, schema="v1"))
    with TestClient(app, follow_redirects=False) as client:
        resp = client.post("/abc123/password", data={"password": "mypassword"})
    assert resp.status_code == 302
    assert "password=mypassword" in resp.headers["location"]


def test_password_form_wrong_password_renders_password_html():
    url_data = _make_url_cache(
        password_hash="mypassword", schema="v1", long_url="https://example.com"
    )
    app = _build_app(_mock_url_service(url_data, schema="v1"))
    with TestClient(app) as client:
        resp = client.post("/abc123/password", data={"password": "wrongpassword"})
    # Re-renders password.html with error — 200 status
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_password_form_url_not_found_returns_400_html():
    url_svc = MagicMock()
    url_svc.resolve = AsyncMock(side_effect=NotFoundError("not found"))
    app = _build_app(url_svc)
    with TestClient(app) as client:
        resp = client.post("/noexist/password", data={"password": "pw"})
    assert resp.status_code == 400
    assert "text/html" in resp.headers["content-type"]


def test_password_form_url_not_password_protected_returns_400_html():
    url_data = _make_url_cache(password_hash=None, long_url="https://example.com")
    app = _build_app(_mock_url_service(url_data))
    with TestClient(app) as client:
        resp = client.post("/abc1234/password", data={"password": "pw"})
    assert resp.status_code == 400
    assert "text/html" in resp.headers["content-type"]


# ── A/B variant tests ─────────────────────────────────────────────────────────

AB_VARIANTS = [
    AbVariant(url="https://example.com/b", weight=60),
    AbVariant(url="https://example.com/c", weight=30),
]


class TestAbVariantRedirect:
    def _app(self, url_data, sink=None):
        return _build_app(_mock_url_service(url_data), sink)

    def _get(self, client, method="get"):
        return getattr(client, method)("/abc1234", headers={"User-Agent": BROWSER_UA})

    @pytest.mark.parametrize(
        ("roll", "location", "index"),
        [
            (0, "https://example.com/b", 0),
            (59, "https://example.com/b", 0),
            (60, "https://example.com/c", 1),
            (89, "https://example.com/c", 1),
            (90, "https://example.com/default", None),
            (99, "https://example.com/default", None),
        ],
    )
    def test_roll_picks_variant_and_stamps_event(
        self, monkeypatch, roll, location, index
    ):
        monkeypatch.setattr("app.routes.redirect_routes.randrange", lambda n: roll)
        url_data = _make_url_cache(
            long_url="https://example.com/default", ab_variants=AB_VARIANTS
        )
        sink = _mock_click_sink()
        with TestClient(self._app(url_data, sink), follow_redirects=False) as client:
            resp = self._get(client)
        assert resp.status_code == 302
        assert resp.headers["location"] == location
        assert resp.headers["cache-control"] == "no-store"
        event = sink.emit.await_args.args[0]
        assert event.variant_index == index

    def test_split_over_many_requests_is_roughly_the_weights(self):
        url_data = _make_url_cache(
            long_url="https://example.com/default", ab_variants=AB_VARIANTS
        )
        sink = _mock_click_sink()
        with TestClient(self._app(url_data, sink), follow_redirects=False) as client:
            locations = [self._get(client).headers["location"] for _ in range(400)]
        b = locations.count("https://example.com/b")
        c = locations.count("https://example.com/c")
        d = locations.count("https://example.com/default")
        # 60/30/10 with a generous band; a broken pick lands far outside it.
        assert 190 <= b <= 290, b
        assert 80 <= c <= 160, c
        assert 15 <= d <= 70, d

    def test_matched_geo_rule_wins_over_variants(self, monkeypatch):
        from app.dependencies import get_geoip_service

        monkeypatch.setattr("app.routes.redirect_routes.randrange", lambda n: 0)
        url_data = _make_url_cache(
            long_url="https://example.com/default",
            geo_rules={"IN": "https://example.in/"},
            ab_variants=AB_VARIANTS,
        )
        sink = _mock_click_sink()
        app = build_test_app(
            redirect_router,
            overrides={
                get_url_service: lambda: _mock_url_service(url_data),
                get_click_sink: lambda: sink,
                get_geoip_service: lambda: MagicMock(),
            },
        )
        with TestClient(app, follow_redirects=False) as client:
            resp = client.get(
                "/abc1234", headers={"User-Agent": BROWSER_UA, "CF-IPCountry": "IN"}
            )
        assert resp.headers["location"] == "https://example.in/"
        event = sink.emit.await_args.args[0]
        assert event.geo_matched is True
        assert event.variant_index is None

    def test_head_request_picks_a_variant_without_event(self, monkeypatch):
        monkeypatch.setattr("app.routes.redirect_routes.randrange", lambda n: 0)
        url_data = _make_url_cache(ab_variants=AB_VARIANTS)
        sink = _mock_click_sink()
        with TestClient(self._app(url_data, sink), follow_redirects=False) as client:
            resp = self._get(client, "head")
        assert resp.status_code == 302
        assert resp.headers["location"] == "https://example.com/b"
        sink.emit.assert_not_awaited()

    def test_plain_link_has_no_variant_and_stays_cacheable(self):
        url_data = _make_url_cache(long_url="https://example.com/target")
        sink = _mock_click_sink()
        with TestClient(self._app(url_data, sink), follow_redirects=False) as client:
            resp = self._get(client)
        assert resp.headers["location"] == "https://example.com/target"
        assert "cache-control" not in resp.headers
        assert sink.emit.await_args.args[0].variant_index is None


# ── Geo-targeting tests ───────────────────────────────────────────────────────

GEO_RULES = {"IN": "https://example.in/", "US": "https://example.com/us"}


def _mock_geoip(country_code=None):
    geoip = MagicMock()
    geoip.get_country_code = AsyncMock(return_value=country_code)
    return geoip


def _build_geo_app(url_svc, click_sink=None, geoip=None):
    from app.dependencies import get_geoip_service

    return build_test_app(
        redirect_router,
        overrides={
            get_url_service: lambda: url_svc,
            get_click_sink: lambda: click_sink or _mock_click_sink(),
            get_geoip_service: lambda: geoip or _mock_geoip(),
        },
    )


class TestGeoTargetedRedirect:
    def test_matching_country_header_redirects_to_rule_url(self):
        url_data = _make_url_cache(
            long_url="https://example.com/default", geo_rules=GEO_RULES
        )
        sink = _mock_click_sink()
        app = _build_geo_app(_mock_url_service(url_data), sink)
        with TestClient(app, follow_redirects=False) as client:
            resp = client.get(
                "/abc1234",
                headers={"User-Agent": BROWSER_UA, "CF-IPCountry": "IN"},
            )
        assert resp.status_code == 302
        assert resp.headers["location"] == "https://example.in/"
        assert resp.headers["cache-control"] == "no-store"
        event = sink.emit.await_args.args[0]
        assert event.resolved_country == "IN"
        assert event.geo_matched is True

    def test_unmatched_country_falls_back_to_default(self):
        url_data = _make_url_cache(
            long_url="https://example.com/default", geo_rules=GEO_RULES
        )
        sink = _mock_click_sink()
        app = _build_geo_app(_mock_url_service(url_data), sink)
        with TestClient(app, follow_redirects=False) as client:
            resp = client.get(
                "/abc1234",
                headers={"User-Agent": BROWSER_UA, "CF-IPCountry": "DE"},
            )
        assert resp.status_code == 302
        assert resp.headers["location"] == "https://example.com/default"
        # Still varies by country → still uncacheable
        assert resp.headers["cache-control"] == "no-store"
        event = sink.emit.await_args.args[0]
        assert event.resolved_country == "DE"
        assert event.geo_matched is False

    def test_lowercase_header_is_normalised(self):
        url_data = _make_url_cache(geo_rules=GEO_RULES)
        app = _build_geo_app(_mock_url_service(url_data))
        with TestClient(app, follow_redirects=False) as client:
            resp = client.get(
                "/abc1234",
                headers={"User-Agent": BROWSER_UA, "CF-IPCountry": "in"},
            )
        assert resp.headers["location"] == "https://example.in/"

    def test_unknown_country_xx_falls_back_to_default(self):
        url_data = _make_url_cache(
            long_url="https://example.com/default", geo_rules=GEO_RULES
        )
        app = _build_geo_app(_mock_url_service(url_data))
        with TestClient(app, follow_redirects=False) as client:
            resp = client.get(
                "/abc1234",
                headers={"User-Agent": BROWSER_UA, "CF-IPCountry": "XX"},
            )
        assert resp.headers["location"] == "https://example.com/default"

    def test_missing_header_falls_back_to_mmdb_lookup(self):
        url_data = _make_url_cache(geo_rules=GEO_RULES)
        geoip = _mock_geoip(country_code="US")
        app = _build_geo_app(_mock_url_service(url_data), geoip=geoip)
        with TestClient(app, follow_redirects=False) as client:
            resp = client.get("/abc1234", headers={"User-Agent": BROWSER_UA})
        assert resp.headers["location"] == "https://example.com/us"
        geoip.get_country_code.assert_awaited_once()

    def test_unresolvable_country_falls_back_to_default(self):
        url_data = _make_url_cache(
            long_url="https://example.com/default", geo_rules=GEO_RULES
        )
        geoip = _mock_geoip(country_code=None)
        app = _build_geo_app(_mock_url_service(url_data), geoip=geoip)
        with TestClient(app, follow_redirects=False) as client:
            resp = client.get("/abc1234", headers={"User-Agent": BROWSER_UA})
        assert resp.headers["location"] == "https://example.com/default"

    def test_head_request_gets_geo_correct_location_without_event(self):
        url_data = _make_url_cache(geo_rules=GEO_RULES)
        sink = _mock_click_sink()
        app = _build_geo_app(_mock_url_service(url_data), sink)
        with TestClient(app, follow_redirects=False) as client:
            resp = client.head(
                "/abc1234",
                headers={"User-Agent": BROWSER_UA, "CF-IPCountry": "IN"},
            )
        assert resp.status_code == 302
        assert resp.headers["location"] == "https://example.in/"
        sink.emit.assert_not_awaited()

    def test_non_geo_link_skips_geoip_and_cache_header(self):
        url_data = _make_url_cache(long_url="https://example.com/target")
        geoip = _mock_geoip()
        app = _build_geo_app(_mock_url_service(url_data), geoip=geoip)
        with TestClient(app, follow_redirects=False) as client:
            resp = client.get("/abc1234", headers={"User-Agent": BROWSER_UA})
        assert resp.headers["location"] == "https://example.com/target"
        assert "cache-control" not in resp.headers
        geoip.get_country_code.assert_not_awaited()


# ── Scheduled links: not live yet ────────────────────────────────────────────


def test_redirect_not_yet_live_returns_404_with_own_slug_and_no_store():
    url_svc = MagicMock()
    url_svc.resolve = AsyncMock(side_effect=NotYetLiveError("not live"))
    app = _build_app(url_svc)
    with TestClient(app) as client:
        resp = client.get("/soon1")
    assert resp.status_code == 404
    assert resp.headers["X-Error-Code"] == "not_yet_live"
    assert resp.headers["Cache-Control"] == "no-store"
    assert "text/html" in resp.headers["content-type"]


def test_redirect_not_yet_live_with_fallback_redirects_no_store():
    url_svc = MagicMock()
    url_svc.resolve = AsyncMock(
        side_effect=NotYetLiveError(
            "not live", fallback_url="https://example.org/coming-soon"
        )
    )
    click_sink = _mock_click_sink()
    app = _build_app(url_svc, click_sink)
    with TestClient(app) as client:
        resp = client.get("/soon2", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://example.org/coming-soon"
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["X-Robots-Tag"] == "noindex, nofollow, noarchive"
    # A pre-start visit is never a click.
    click_sink.emit.assert_not_awaited()


def test_head_not_yet_live_matches_get():
    url_svc = MagicMock()
    url_svc.resolve = AsyncMock(side_effect=NotYetLiveError("not live"))
    app = _build_app(url_svc)
    with TestClient(app) as client:
        resp = client.head("/soon3")
    assert resp.status_code == 404
    assert resp.headers["X-Error-Code"] == "not_yet_live"


def test_redirect_not_yet_live_edge_composed_returns_empty_body(edge_composed_errors):
    url_svc = MagicMock()
    url_svc.resolve = AsyncMock(side_effect=NotYetLiveError("not live"))
    app = _build_app(url_svc)
    with TestClient(app) as client:
        resp = client.get("/soon4")
    assert resp.status_code == 404
    assert resp.headers["X-Error-Code"] == "not_yet_live"
    assert resp.content == b""


def test_password_form_not_reached_before_start():
    """A scheduled password link answers not-yet-live, never the form."""
    url_svc = MagicMock()
    url_svc.resolve = AsyncMock(side_effect=NotYetLiveError("not live"))
    app = _build_app(url_svc)
    with TestClient(app) as client:
        resp = client.post("/soon5/password", data={"password": "x"})
    assert resp.status_code == 400
