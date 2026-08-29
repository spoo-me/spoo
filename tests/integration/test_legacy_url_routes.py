"""
Integration tests for legacy URL shortener routes.

Covers: GET /, POST / (v1 shorten), POST /emoji, GET /result/<code>,
        GET /<code>+ (preview), GET /metric.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/")

from bson import ObjectId

from config import AppSettings
from dependencies import (
    get_current_user,
    get_db,
    get_redis,
    get_settings,
    get_url_policy,
    get_url_service,
)
from errors import NotFoundError
from infrastructure.cache.url_cache import UrlCacheData
from middleware.error_handler import register_error_handlers
from middleware.rate_limiter import limiter
from routes.legacy.url_shortener import router as legacy_url_router
from tests.conftest import build_test_app

# ── Helpers ──────────────────────────────────────────────────────────────────

_STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "static"
)


def _make_gate(blocked_repo):
    """Real L0 gate over a mocked pattern repo (ttl=0 = always fresh)."""
    from services.safety.policy import UrlPolicyService
    from services.safety.providers import BlockedPatternProvider

    return UrlPolicyService(
        [
            BlockedPatternProvider(
                blocked_repo, regex_timeout=0.2, patterns_ttl_seconds=0
            )
        ],
        blocked_self_domains=["spoo.me"],
    )


def _mock_db():
    """Return a mock MongoDB database with collection subscript support."""
    db = MagicMock()
    return db


def _mock_settings():
    settings = AppSettings()
    return settings


def _mock_url_service(url_data=None, schema="v2"):
    svc = MagicMock()
    if url_data is not None:
        svc.resolve = AsyncMock(return_value=(url_data, schema))
    else:
        svc.resolve = AsyncMock(side_effect=NotFoundError("not found"))
    svc.check_alias_available = AsyncMock(return_value=True)
    return svc


def _make_url_cache(
    alias: str = "abc1234",
    long_url: str = "https://example.com",
    schema: str = "v2",
) -> UrlCacheData:
    return UrlCacheData(
        id="507f1f77bcf86cd799439011",
        alias=alias,
        long_url=long_url,
        block_bots=False,
        password_hash=None,
        expiration_time=None,
        max_clicks=None,
        url_status="ACTIVE",
        schema_version=schema,
        owner_id=None,
        total_clicks=0,
    )


# ── Index tests ──────────────────────────────────────────────────────────────


def test_index_renders_html():
    app = build_test_app(legacy_url_router, overrides={})
    with TestClient(app) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_index_redirects_authenticated_user_to_dashboard():
    from dependencies.auth import CurrentUser

    user = CurrentUser(user_id=ObjectId(), email_verified=True)
    app = build_test_app(legacy_url_router, overrides={get_current_user: lambda: user})
    with TestClient(app, follow_redirects=False) as client:
        resp = client.get("/")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/dashboard"


# ── POST / (legacy shorten) tests ────────────────────────────────────────────


def test_shorten_url_json_missing_url_returns_400():
    db = _mock_db()
    app = build_test_app(
        legacy_url_router,
        overrides={
            get_db: lambda: db,
            get_settings: lambda: _mock_settings(),
            get_url_service: lambda: _mock_url_service(),
        },
    )
    with TestClient(app) as client:
        resp = client.post("/", data={}, headers={"Accept": "application/json"})
    assert resp.status_code == 400
    assert "UrlError" in resp.json()


def test_shorten_url_html_missing_url_returns_400():
    db = _mock_db()
    app = build_test_app(
        legacy_url_router,
        overrides={
            get_db: lambda: db,
            get_settings: lambda: _mock_settings(),
            get_url_service: lambda: _mock_url_service(),
        },
    )
    with TestClient(app) as client:
        resp = client.post("/", data={})
    assert resp.status_code == 400
    assert "text/html" in resp.headers["content-type"]


def test_shorten_url_json_success():
    """POST / with Accept: application/json returns JSON with short_url."""
    db = _mock_db()
    # Mock blocked URL repo to return no patterns
    blocked_col = MagicMock()
    blocked_col.find = MagicMock(return_value=MagicMock())
    blocked_col.find.return_value.to_list = AsyncMock(return_value=[])
    db.__getitem__ = MagicMock(return_value=blocked_col)

    # Mock the repos for alias check and insert
    url_svc = _mock_url_service()

    settings = _mock_settings()

    app = build_test_app(
        legacy_url_router,
        overrides={
            get_db: lambda: db,
            get_settings: lambda: settings,
            get_url_service: lambda: url_svc,
        },
    )

    # Patch the BlockedUrlRepository.get_patterns and LegacyUrlRepository
    with (
        patch("routes.legacy.url_shortener.LegacyUrlRepository") as MockLegacyRepo,
        patch("routes.legacy.url_shortener.UrlRepository") as MockUrlRepo,
    ):
        MockLegacyRepo.return_value.check_exists = AsyncMock(return_value=False)
        MockLegacyRepo.return_value.insert = AsyncMock(return_value=None)
        MockUrlRepo.return_value.check_alias_exists = AsyncMock(return_value=False)

        with TestClient(app) as client:
            resp = client.post(
                "/",
                data={"url": "https://example.com"},
                headers={"Accept": "application/json"},
            )
    assert resp.status_code == 200
    data = resp.json()
    assert "short_url" in data
    assert "original_url" in data
    assert data["original_url"] == "https://example.com"


def test_shorten_url_stamps_destination_parts():
    """v1 creates carry the same parsed dest subdoc as v2, so safety
    sweeps and takedown pivots cover the legacy collection."""
    db = _mock_db()
    blocked_col = MagicMock()
    blocked_col.find = MagicMock(return_value=MagicMock())
    blocked_col.find.return_value.to_list = AsyncMock(return_value=[])
    db.__getitem__ = MagicMock(return_value=blocked_col)

    url_svc = _mock_url_service()
    settings = _mock_settings()

    app = build_test_app(
        legacy_url_router,
        overrides={
            get_db: lambda: db,
            get_settings: lambda: settings,
            get_url_service: lambda: url_svc,
        },
    )

    with (
        patch("routes.legacy.url_shortener.LegacyUrlRepository") as MockLegacyRepo,
        patch("routes.legacy.url_shortener.UrlRepository") as MockUrlRepo,
    ):
        MockLegacyRepo.return_value.check_exists = AsyncMock(return_value=False)
        MockLegacyRepo.return_value.insert = AsyncMock(return_value=None)
        MockUrlRepo.return_value.check_alias_exists = AsyncMock(return_value=False)

        with TestClient(app) as client:
            resp = client.post(
                "/",
                data={"url": "https://a.evil.co.uk/kit"},
                headers={"Accept": "application/json"},
            )
        assert resp.status_code == 200
        inserted = MockLegacyRepo.return_value.insert.await_args.args[1]
    assert inserted["dest"]["registrable_domain"] == "evil.co.uk"
    assert inserted["dest"]["host"] == "a.evil.co.uk"


def test_shorten_url_html_success_redirects_to_result():
    """POST / without Accept: application/json redirects to /result/."""
    db = _mock_db()
    url_svc = _mock_url_service()
    settings = _mock_settings()

    app = build_test_app(
        legacy_url_router,
        overrides={
            get_db: lambda: db,
            get_settings: lambda: settings,
            get_url_service: lambda: url_svc,
        },
    )

    with (
        patch("routes.legacy.url_shortener.LegacyUrlRepository") as MockLegacyRepo,
        patch("routes.legacy.url_shortener.UrlRepository") as MockUrlRepo,
    ):
        MockLegacyRepo.return_value.check_exists = AsyncMock(return_value=False)
        MockLegacyRepo.return_value.insert = AsyncMock(return_value=None)
        MockUrlRepo.return_value.check_alias_exists = AsyncMock(return_value=False)

        with TestClient(app, follow_redirects=False) as client:
            resp = client.post("/", data={"url": "https://example.com"})
    assert resp.status_code == 302
    assert "/result/" in resp.headers["location"]


def test_shorten_url_blocked_url_returns_403():
    db = _mock_db()
    settings = _mock_settings()
    url_svc = _mock_url_service()

    app = build_test_app(
        legacy_url_router,
        overrides={
            get_db: lambda: db,
            get_settings: lambda: settings,
            get_url_service: lambda: url_svc,
        },
    )

    blocked_repo = MagicMock()
    blocked_repo.get_patterns = AsyncMock(return_value=[r"evil\.com"])
    app.dependency_overrides[get_url_policy] = lambda: _make_gate(blocked_repo)

    with TestClient(app) as client:
        resp = client.post(
            "/",
            data={"url": "https://evil.com"},
            headers={"Accept": "application/json"},
        )
    assert resp.status_code == 403
    assert "BlockedUrlError" in resp.json()


def test_shorten_url_published_policy_message_reaches_the_wire():
    """The published shortener-refusal message survives the legacy route."""
    from services.safety.feeds import SHORTENER_FEED
    from services.safety.policy import UrlPolicyService
    from services.safety.providers import FeedDomainProvider

    db = _mock_db()
    settings = _mock_settings()
    url_svc = _mock_url_service()

    app = build_test_app(
        legacy_url_router,
        overrides={
            get_db: lambda: db,
            get_settings: lambda: settings,
            get_url_service: lambda: url_svc,
        },
    )

    feed_repo = MagicMock()
    feed_repo.contains = AsyncMock(return_value=True)
    gate = UrlPolicyService(
        [
            FeedDomainProvider(
                feed_repo, feed=SHORTENER_FEED, reason_label="a link shortener"
            )
        ],
        blocked_self_domains=["spoo.me"],
        public_messages={
            f"feed_{SHORTENER_FEED}": "Links to other URL shorteners are not allowed"
        },
    )
    app.dependency_overrides[get_url_policy] = lambda: gate

    with TestClient(app) as client:
        resp = client.post(
            "/",
            data={"url": "https://bit.ly/abc"},
            headers={"Accept": "application/json"},
        )
    assert resp.status_code == 403
    assert resp.json()["BlockedUrlError"] == (
        "Links to other URL shorteners are not allowed"
    )


def test_shorten_url_invalid_alias_returns_400():
    db = _mock_db()
    settings = _mock_settings()
    url_svc = _mock_url_service()

    app = build_test_app(
        legacy_url_router,
        overrides={
            get_db: lambda: db,
            get_settings: lambda: settings,
            get_url_service: lambda: url_svc,
        },
    )

    with TestClient(app) as client:
        resp = client.post(
            "/",
            data={"url": "https://example.com", "alias": "bad alias!!!"},
            headers={"Accept": "application/json"},
        )
    assert resp.status_code == 400
    assert "AliasError" in resp.json()


def test_shorten_url_alias_already_exists_returns_400():
    db = _mock_db()
    settings = _mock_settings()
    url_svc = MagicMock()
    url_svc.check_alias_available = AsyncMock(return_value=False)

    app = build_test_app(
        legacy_url_router,
        overrides={
            get_db: lambda: db,
            get_settings: lambda: settings,
            get_url_service: lambda: url_svc,
        },
    )

    with TestClient(app) as client:
        resp = client.post(
            "/",
            data={"url": "https://example.com", "alias": "taken"},
            headers={"Accept": "application/json"},
        )
    assert resp.status_code == 400
    assert "AliasError" in resp.json()


def test_shorten_url_invalid_password_returns_400():
    db = _mock_db()
    settings = _mock_settings()
    url_svc = _mock_url_service()

    app = build_test_app(
        legacy_url_router,
        overrides={
            get_db: lambda: db,
            get_settings: lambda: settings,
            get_url_service: lambda: url_svc,
        },
    )

    with (
        patch("routes.legacy.url_shortener.LegacyUrlRepository") as MockLegacyRepo,
        patch("routes.legacy.url_shortener.UrlRepository") as MockUrlRepo,
    ):
        MockLegacyRepo.return_value.check_exists = AsyncMock(return_value=False)
        MockUrlRepo.return_value.check_alias_exists = AsyncMock(return_value=False)

        with TestClient(app) as client:
            resp = client.post(
                "/",
                data={"url": "https://example.com", "password": "short"},
                headers={"Accept": "application/json"},
            )
    assert resp.status_code == 400
    assert "PasswordError" in resp.json()


def test_shorten_url_invalid_max_clicks_returns_400():
    db = _mock_db()
    settings = _mock_settings()
    url_svc = _mock_url_service()

    app = build_test_app(
        legacy_url_router,
        overrides={
            get_db: lambda: db,
            get_settings: lambda: settings,
            get_url_service: lambda: url_svc,
        },
    )

    with (
        patch("routes.legacy.url_shortener.LegacyUrlRepository") as MockLegacyRepo,
        patch("routes.legacy.url_shortener.UrlRepository") as MockUrlRepo,
    ):
        MockLegacyRepo.return_value.check_exists = AsyncMock(return_value=False)
        MockUrlRepo.return_value.check_alias_exists = AsyncMock(return_value=False)

        with TestClient(app) as client:
            resp = client.post(
                "/",
                data={"url": "https://example.com", "max-clicks": "notanumber"},
                headers={"Accept": "application/json"},
            )
    assert resp.status_code == 400
    assert "MaxClicksError" in resp.json()


# ── POST /emoji tests ────────────────────────────────────────────────────────


def test_emoji_missing_url_returns_400():
    db = _mock_db()
    settings = _mock_settings()
    app = build_test_app(
        legacy_url_router,
        overrides={
            get_db: lambda: db,
            get_settings: lambda: settings,
        },
    )
    with TestClient(app) as client:
        resp = client.post("/emoji", data={})
    assert resp.status_code == 400
    assert "UrlError" in resp.json()


def test_emoji_get_without_url_returns_400():
    """GET /emoji with no query params returns 400 (matches Flask behavior)."""
    db = _mock_db()
    settings = _mock_settings()
    app = build_test_app(
        legacy_url_router,
        overrides={
            get_db: lambda: db,
            get_settings: lambda: settings,
        },
    )
    with TestClient(app) as client:
        resp = client.get("/emoji")
    assert resp.status_code == 400
    assert "UrlError" in resp.json()


def test_emoji_success_json():
    db = _mock_db()
    settings = _mock_settings()

    app = build_test_app(
        legacy_url_router,
        overrides={
            get_db: lambda: db,
            get_settings: lambda: settings,
        },
    )

    with (
        patch("routes.legacy.url_shortener.EmojiUrlRepository") as MockEmojiRepo,
    ):
        MockEmojiRepo.return_value.check_exists = AsyncMock(return_value=False)
        MockEmojiRepo.return_value.insert = AsyncMock(return_value=None)

        with TestClient(app) as client:
            resp = client.post(
                "/emoji",
                data={"url": "https://example.com"},
                headers={"Accept": "application/json"},
            )
    assert resp.status_code == 200
    data = resp.json()
    assert "short_url" in data
    assert "original_url" in data


# ── GET /result/<code> tests ─────────────────────────────────────────────────


def test_result_page_found():
    url_data = _make_url_cache(alias="abc1234", long_url="https://example.com")
    url_svc = _mock_url_service(url_data)
    app = build_test_app(
        legacy_url_router, overrides={get_url_service: lambda: url_svc}
    )
    with TestClient(app) as client:
        resp = client.get("/result/abc1234")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_result_page_not_found():
    url_svc = _mock_url_service()  # side_effect=NotFoundError
    app = build_test_app(
        legacy_url_router, overrides={get_url_service: lambda: url_svc}
    )
    with TestClient(app) as client:
        resp = client.get("/result/doesnotexist")
    assert resp.status_code == 404
    assert "text/html" in resp.headers["content-type"]


# ── GET /<code>+ (preview) tests ─────────────────────────────────────────────


def test_preview_not_found():
    db = _mock_db()
    # Mock repos to return None
    with (
        patch("routes.legacy.url_shortener.UrlRepository") as MockUrlRepo,
        patch("routes.legacy.url_shortener.LegacyUrlRepository") as MockLegacyRepo,
    ):
        MockUrlRepo.return_value.find_by_alias = AsyncMock(return_value=None)
        MockLegacyRepo.return_value.find_by_id = AsyncMock(return_value=None)

        app = build_test_app(legacy_url_router, overrides={get_db: lambda: db})
        with TestClient(app) as client:
            resp = client.get("/abc1234+")
    assert resp.status_code == 404
    assert "text/html" in resp.headers["content-type"]


def test_preview_v2_url_shows_destination():
    db = _mock_db()
    v2_doc = MagicMock()
    v2_doc.alias = "abc1234"
    v2_doc.long_url = "https://example.com/page"
    v2_doc.password = None

    with (
        patch("routes.legacy.url_shortener.UrlRepository") as MockUrlRepo,
        patch("routes.legacy.url_shortener.LegacyUrlRepository") as MockLegacyRepo,
    ):
        MockUrlRepo.return_value.find_by_alias = AsyncMock(return_value=v2_doc)
        MockLegacyRepo.return_value.find_by_id = AsyncMock(return_value=None)

        app = build_test_app(legacy_url_router, overrides={get_db: lambda: db})
        with TestClient(app) as client:
            resp = client.get("/abc1234+")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_preview_shows_custom_meta_card_next_to_destination():
    """og-links: the + page shows BOTH the owner's card and the real
    destination — the anti-phishing transparency surface."""
    from schemas.models.url import LinkMetaTags

    db = _mock_db()
    v2_doc = MagicMock()
    v2_doc.alias = "abc1234"
    v2_doc.long_url = "https://example.com/page"
    v2_doc.password = None
    v2_doc.meta_tags = LinkMetaTags(
        title="Custom Card Title", description="Custom desc", color="#FF5733"
    )

    with (
        patch("routes.legacy.url_shortener.UrlRepository") as MockUrlRepo,
        patch("routes.legacy.url_shortener.LegacyUrlRepository") as MockLegacyRepo,
    ):
        MockUrlRepo.return_value.find_by_alias = AsyncMock(return_value=v2_doc)
        MockLegacyRepo.return_value.find_by_id = AsyncMock(return_value=None)

        app = build_test_app(legacy_url_router, overrides={get_db: lambda: db})
        with TestClient(app) as client:
            resp = client.get("/abc1234+")
    assert resp.status_code == 200
    assert "Custom Card Title" in resp.text  # what the sender wants shown
    # The real destination host, as rendered in the domain-name span —
    # exact markup asserted (not a URL substring; keeps CodeQL quiet).
    assert ">example.com<" in resp.text
    assert "set by link owner" in resp.text


def test_preview_password_protected_hides_destination():
    db = _mock_db()
    v2_doc = MagicMock()
    v2_doc.alias = "abc1234"
    v2_doc.long_url = "https://secret.example.com"
    v2_doc.password = "hashed_password"

    with (
        patch("routes.legacy.url_shortener.UrlRepository") as MockUrlRepo,
        patch("routes.legacy.url_shortener.LegacyUrlRepository") as MockLegacyRepo,
    ):
        MockUrlRepo.return_value.find_by_alias = AsyncMock(return_value=v2_doc)
        MockLegacyRepo.return_value.find_by_id = AsyncMock(return_value=None)

        app = build_test_app(legacy_url_router, overrides={get_db: lambda: db})
        with TestClient(app) as client:
            resp = client.get("/abc1234+")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


# ── GET /metric tests ────────────────────────────────────────────────────────


def test_metric_returns_json():
    """GET /metric returns JSON with the expected shape (bypasses DualCache when no Redis)."""
    # Build a dict-backed mock DB so db["urls"] returns the same object each time
    urls_col = MagicMock()
    cursor = MagicMock()
    cursor.to_list = AsyncMock(
        return_value=[{"total-shortlinks": 100, "total-clicks": 5000}]
    )
    urls_col.aggregate = AsyncMock(return_value=cursor)

    urlsv2_col = MagicMock()
    urlsv2_col.estimated_document_count = AsyncMock(return_value=50)

    clicks_col = MagicMock()
    clicks_col.estimated_document_count = AsyncMock(return_value=3000)

    collections = {"urls": urls_col, "urlsV2": urlsv2_col, "clicks": clicks_col}
    db = MagicMock()
    db.__getitem__ = MagicMock(side_effect=lambda k: collections.get(k, MagicMock()))

    # Mock HTTP client for GitHub stars
    http_client = MagicMock()
    gh_resp = MagicMock()
    gh_resp.status_code = 200
    gh_resp.json.return_value = {"stargazers_count": 42}
    http_client.get = AsyncMock(return_value=gh_resp)

    settings = _mock_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.db = db
        app.state.redis = None  # No Redis → DualCache calls query directly
        app.state.email_provider = MagicMock()
        app.state.http_client = http_client
        app.state.oauth_providers = {}
        yield

    app = FastAPI(lifespan=lifespan)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    register_error_handlers(app)
    if os.path.isdir(_STATIC_DIR):
        app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    app.include_router(legacy_url_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_redis] = lambda: None

    with TestClient(app) as client:
        resp = client.get("/metric")
    assert resp.status_code == 200
    data = resp.json()
    assert "total-shortlinks-raw" in data
    assert "total-clicks-raw" in data
    assert "total-shortlinks" in data
    assert "total-clicks" in data
    assert "github-stars" in data
    assert data["total-shortlinks-raw"] == 150  # 100 + 50
    assert data["total-clicks-raw"] == 8000  # 5000 + 3000
    assert data["github-stars"] == 42


def test_preview_geo_link_lists_every_destination():
    """The preview page is the anti-cloaking surface — every geo destination
    must be visible, plus the default, plus the variance banner."""
    db = _mock_db()
    v2_doc = MagicMock()
    v2_doc.alias = "abc1234"
    v2_doc.long_url = "https://example.com/default"
    v2_doc.password = None
    v2_doc.geo_rules = {
        "IN": "https://example.in/offer",
        "BD": "https://example.in/offer",
        "US": "https://us.example.com/",
    }

    with (
        patch("routes.legacy.url_shortener.UrlRepository") as MockUrlRepo,
        patch("routes.legacy.url_shortener.LegacyUrlRepository") as MockLegacyRepo,
    ):
        MockUrlRepo.return_value.find_by_alias = AsyncMock(return_value=v2_doc)
        MockLegacyRepo.return_value.find_by_id = AsyncMock(return_value=None)

        app = build_test_app(legacy_url_router, overrides={get_db: lambda: db})
        with TestClient(app) as client:
            resp = client.get("/abc1234+")

    assert resp.status_code == 200
    html = resp.text
    assert "different destinations depending on the visitor" in html
    assert "Default destination:" in html
    assert "example.com" in html  # default
    assert "example.in" in html
    assert "us.example.com" in html
    # Countries sharing a destination are grouped into one row, sorted
    assert "BD, IN" in html
    assert "US:" in html


def test_preview_non_geo_link_has_no_banner():
    db = _mock_db()
    v2_doc = MagicMock()
    v2_doc.alias = "abc1234"
    v2_doc.long_url = "https://example.com/page"
    v2_doc.password = None
    v2_doc.geo_rules = None

    with (
        patch("routes.legacy.url_shortener.UrlRepository") as MockUrlRepo,
        patch("routes.legacy.url_shortener.LegacyUrlRepository") as MockLegacyRepo,
    ):
        MockUrlRepo.return_value.find_by_alias = AsyncMock(return_value=v2_doc)
        MockLegacyRepo.return_value.find_by_id = AsyncMock(return_value=None)

        app = build_test_app(legacy_url_router, overrides={get_db: lambda: db})
        with TestClient(app) as client:
            resp = client.get("/abc1234+")

    assert resp.status_code == 200
    assert "different destinations" not in resp.text
    assert "Redirects to:" in resp.text


def test_preview_password_protected_geo_link_hides_everything():
    """Password precedence: the protected branch renders first, so geo
    destinations stay hidden along with the default."""
    db = _mock_db()
    v2_doc = MagicMock()
    v2_doc.alias = "abc1234"
    v2_doc.long_url = "https://secret.example.com"
    v2_doc.password = "hashed_password"
    v2_doc.geo_rules = {"IN": "https://secret.example.in/"}

    with (
        patch("routes.legacy.url_shortener.UrlRepository") as MockUrlRepo,
        patch("routes.legacy.url_shortener.LegacyUrlRepository") as MockLegacyRepo,
    ):
        MockUrlRepo.return_value.find_by_alias = AsyncMock(return_value=v2_doc)
        MockLegacyRepo.return_value.find_by_id = AsyncMock(return_value=None)

        app = build_test_app(legacy_url_router, overrides={get_db: lambda: db})
        with TestClient(app) as client:
            resp = client.get("/abc1234+")

    assert resp.status_code == 200
    html = resp.text
    assert "hidden for security" in html
    assert "secret.example.in" not in html
    assert "secret.example.com" not in html
