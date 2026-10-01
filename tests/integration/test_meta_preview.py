"""
Integration tests for the custom meta-tags preview branch on the redirect route.

Preview crawlers get a prerendered OG page; humans, search/AI crawlers, and
links without meta_tags keep the 302 byte-for-byte. Preview serves are never
clicks.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from app.dependencies import get_click_sink, get_url_service
from app.infrastructure.cache.url_cache import UrlCacheData
from app.routes.redirect_routes import router as redirect_router
from tests.conftest import build_test_app
from tests.factories import make_url_cache

FB_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
GOOGLEBOT_UA = (
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
)

META = dict(
    meta_title="My Title",
    meta_description="My description",
    meta_image="https://cdn.example.com/og.png",
    meta_color="#FF5733",
)


def _og_link(**overrides) -> UrlCacheData:
    return make_url_cache(**{**META, **overrides})


def _mock_url_service(url_data: UrlCacheData, schema: str = "v2") -> MagicMock:
    svc = MagicMock()
    svc.resolve = AsyncMock(return_value=(url_data, schema))
    return svc


def _mock_click_sink() -> MagicMock:
    sink = MagicMock()
    sink.emit = AsyncMock(return_value=None)
    return sink


def _client(url_data: UrlCacheData, schema: str = "v2", sink=None) -> TestClient:
    app = build_test_app(
        redirect_router,
        overrides={
            get_url_service: lambda: _mock_url_service(url_data, schema),
            get_click_sink: lambda: sink or _mock_click_sink(),
        },
    )
    return TestClient(app, follow_redirects=False, raise_server_exceptions=False)


# ── Preview crawlers get the OG page ─────────────────────────────────────────


def test_preview_bot_gets_og_page():
    with _client(_og_link()) as c:
        resp = c.get("/abc1234", headers={"User-Agent": FB_UA})
    assert resp.status_code == 200
    assert 'property="og:title" content="My Title"' in resp.text
    assert 'property="og:description" content="My description"' in resp.text
    assert 'property="og:image" content="https://cdn.example.com/og.png"' in resp.text
    assert 'property="og:url" content="https://spoo.me/abc1234"' in resp.text
    assert 'name="twitter:title" content="My Title"' in resp.text
    assert 'name="twitter:card" content="summary_large_image"' in resp.text
    assert 'name="theme-color" content="#FF5733"' in resp.text
    assert 'name="robots" content="noindex"' in resp.text
    assert resp.headers["x-robots-tag"] == "noindex, nofollow, noarchive"
    assert 'rel="canonical"' not in resp.text
    assert "/static/" not in resp.text  # self-contained: tenant-safe


def test_preview_page_without_image_uses_summary_card():
    with _client(_og_link(meta_image=None)) as c:
        resp = c.get("/abc1234", headers={"User-Agent": FB_UA})
    assert resp.status_code == 200
    assert 'name="twitter:card" content="summary"' in resp.text
    assert "og:image" not in resp.text


def test_preview_serve_is_not_a_click():
    sink = _mock_click_sink()
    with _client(_og_link(), sink=sink) as c:
        c.get("/abc1234", headers={"User-Agent": FB_UA})
    sink.emit.assert_not_called()


def test_head_gets_og_page():
    with _client(_og_link()) as c:
        resp = c.head("/abc1234", headers={"User-Agent": CHROME_UA})
    assert resp.status_code == 200


def test_bot_param_shows_page_without_autoredirect():
    with _client(_og_link()) as c:
        resp = c.get("/abc1234?bot=1", headers={"User-Agent": CHROME_UA})
    assert resp.status_code == 200
    assert "location.replace" not in resp.text


def test_autoredirect_script_present_for_bots():
    # Misclassified humans self-heal via the JS fallback. Exact rendered
    # script asserted (not a URL substring — keeps CodeQL quiet too).
    with _client(_og_link()) as c:
        resp = c.get("/abc1234", headers={"User-Agent": FB_UA})
    assert 'location.replace("https://example.com")' in resp.text


# ── Everyone else keeps the 302 ──────────────────────────────────────────────


def test_human_gets_302_and_click():
    sink = _mock_click_sink()
    with _client(_og_link(), sink=sink) as c:
        resp = c.get("/abc1234", headers={"User-Agent": CHROME_UA})
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://example.com"
    sink.emit.assert_called_once()


def test_googlebot_gets_302():
    with _client(_og_link()) as c:
        resp = c.get("/abc1234", headers={"User-Agent": GOOGLEBOT_UA})
    assert resp.status_code == 302


def test_preview_bot_on_plain_link_redirects():
    # No meta_tags → byte-for-byte today's behavior.
    with _client(make_url_cache()) as c:
        resp = c.get("/abc1234", headers={"User-Agent": FB_UA})
    assert resp.status_code == 302


def test_bot_param_on_plain_link_redirects():
    with _client(make_url_cache()) as c:
        resp = c.get("/abc1234?bot=1", headers={"User-Agent": CHROME_UA})
    assert resp.status_code == 302


def test_v1_link_with_meta_fields_never_serves_preview():
    # meta_tags is v2-only; a v1 resolve must never hit the branch even if
    # cache data somehow carried meta fields.
    with _client(_og_link(schema_version="v1"), schema="v1") as c:
        resp = c.get("/abc1234", headers={"User-Agent": FB_UA})
    assert resp.status_code == 302


# ── Interactions with password / block_bots ──────────────────────────────────


def test_password_og_link_serves_page_to_bots():
    # Bots get the owner's card instead of the 401 password page; no click.
    # The destination must NOT appear — the preview branch runs before the
    # password gate, so revealing it would bypass password protection.
    sink = _mock_click_sink()
    link = _og_link(
        password_hash="$argon2id$fake",
        long_url="https://secret-destination.example/hidden",
        meta_image=None,
    )
    with _client(link, sink=sink) as c:
        resp = c.get("/abc1234", headers={"User-Agent": FB_UA})
    assert resp.status_code == 200
    assert "secret-destination" not in resp.text
    assert "location.replace" not in resp.text
    sink.emit.assert_not_called()


def test_password_og_link_still_gates_humans():
    with _client(_og_link(password_hash="$argon2id$fake")) as c:
        resp = c.get("/abc1234", headers={"User-Agent": CHROME_UA})
    assert resp.status_code == 401


def test_block_bots_og_link_omits_destination():
    # Neither the URL nor even the HOSTNAME of a bot-blocked destination
    # may appear anywhere in the served page.
    link = _og_link(
        block_bots=True,
        long_url="https://secret-destination.example/hidden",
        meta_image=None,  # keep the page free of unrelated hostnames
    )
    with _client(link) as c:
        resp = c.get("/abc1234", headers={"User-Agent": FB_UA})
    assert resp.status_code == 200
    assert "secret-destination" not in resp.text
    assert "Preview only." in resp.text
