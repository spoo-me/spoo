"""
Integration tests for legacy URL shortening and stats routes.

POST /              -> legacy v1 shorten
POST /emoji         -> emoji URL creation
GET  /result/{code} -> result page
GET  /metric        -> global metrics
GET  /stats         -> stats form
POST /stats         -> redirect to stats page
GET  /export/{code}/{format} -> export file

All DB / Redis / external-service calls are eliminated via
dependency_overrides and a mock lifespan — no real infrastructure needed.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/")

from config import AppSettings
from dependencies import get_db, get_redis, get_settings, get_url_service
from errors import NotFoundError
from infrastructure.cache.url_cache import UrlCacheData
from middleware.error_handler import register_error_handlers
from middleware.rate_limiter import limiter
from routes.legacy.stats import router as legacy_stats_router
from routes.legacy.url_shortener import router as legacy_url_router
from tests.conftest import build_test_app

# ── Helpers ──────────────────────────────────────────────────────────────────

_STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "static"
)

_SETTINGS = AppSettings()


def _build_legacy_test_app(overrides: dict) -> FastAPI:
    """Build a test app with legacy routers, injecting get_settings override."""
    from dependencies import get_url_policy
    from services.safety.policy import UrlPolicyService

    # Always provide settings + a permissive L0 gate — several tests here
    # never enter the lifespan, so app.state is not populated.
    base_overrides = {
        get_settings: lambda: _SETTINGS,
        get_url_policy: lambda: UrlPolicyService(
            [], blocked_self_domains=[_SETTINGS.system_default_domain]
        ),
    }
    base_overrides.update(overrides)
    return build_test_app(
        legacy_stats_router, legacy_url_router, overrides=base_overrides
    )


@dataclass
class _FakeLegacyDoc:
    """Minimal fake for LegacyUrlDoc / EmojiUrlDoc returned by repository find_by_id."""

    url: str
    password: str | None = None


def _make_mock_db():
    """Create a mock db object with all collections pre-configured."""
    db = MagicMock()

    # blocked-urls collection
    blocked_cursor = MagicMock()
    blocked_cursor.to_list = AsyncMock(return_value=[])
    db["blocked-urls"].find = MagicMock(return_value=blocked_cursor)

    # urls (v1) collection
    db["urls"].find_one = AsyncMock(return_value=None)
    db["urls"].insert_one = AsyncMock(
        return_value=MagicMock(inserted_id=str(ObjectId()))
    )
    db["urls"].count_documents = AsyncMock(return_value=0)
    agg_cursor = MagicMock()
    agg_cursor.to_list = AsyncMock(return_value=[])
    db["urls"].aggregate = AsyncMock(return_value=agg_cursor)

    # urlsV2 collection
    db["urlsV2"].find_one = AsyncMock(return_value=None)
    db["urlsV2"].count_documents = AsyncMock(return_value=0)
    db["urlsV2"].estimated_document_count = AsyncMock(return_value=0)

    # emojis collection
    db["emojis"].find_one = AsyncMock(return_value=None)
    db["emojis"].insert_one = AsyncMock(
        return_value=MagicMock(inserted_id=str(ObjectId()))
    )
    db["emojis"].count_documents = AsyncMock(return_value=0)
    emoji_agg_cursor = MagicMock()
    emoji_agg_cursor.to_list = AsyncMock(return_value=[])
    db["emojis"].aggregate = AsyncMock(return_value=emoji_agg_cursor)

    # clicks collection
    db["clicks"].estimated_document_count = AsyncMock(return_value=0)

    return db


def _make_cache_data(**kwargs) -> UrlCacheData:
    defaults = dict(
        id=str(ObjectId()),
        alias="abc123",
        long_url="https://example.com/destination",
        block_bots=False,
        password_hash=None,
        expiration_time=None,
        max_clicks=None,
        url_status="ACTIVE",
        schema_version="v2",
        owner_id=str(ObjectId()),
        total_clicks=0,
    )
    defaults.update(kwargs)
    return UrlCacheData(**defaults)


# ── POST / (legacy shorten) ─────────────────────────────────────────────────


def test_legacy_shorten_json_response():
    """POST / with Accept: application/json -> JSON with short_url."""
    mock_db = _make_mock_db()
    mock_url_svc = AsyncMock()
    mock_url_svc.check_alias_available = AsyncMock(return_value=True)

    app = _build_legacy_test_app(
        {
            get_db: lambda: mock_db,
            get_url_service: lambda: mock_url_svc,
        }
    )
    client = TestClient(app, raise_server_exceptions=False)

    with (
        patch("routes.legacy.url_shortener.generate_short_code", return_value="gen123"),
    ):
        resp = client.post(
            "/",
            data={"url": "https://example.com/long"},
            headers={"Accept": "application/json"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert "short_url" in body
    assert "original_url" in body
    assert body["original_url"] == "https://example.com/long"


def test_legacy_shorten_html_redirect():
    """POST / without JSON accept -> 302 to /result/{code}."""
    mock_db = _make_mock_db()
    mock_url_svc = AsyncMock()
    mock_url_svc.check_alias_available = AsyncMock(return_value=True)

    app = _build_legacy_test_app(
        {
            get_db: lambda: mock_db,
            get_url_service: lambda: mock_url_svc,
        }
    )
    client = TestClient(app, raise_server_exceptions=False)

    with (
        patch("routes.legacy.url_shortener.generate_short_code", return_value="gen456"),
    ):
        resp = client.post(
            "/",
            data={"url": "https://example.com/long"},
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )

    assert resp.status_code == 302
    assert "/result/" in resp.headers["Location"]


def test_legacy_shorten_missing_url():
    """POST / without url -> 400 with UrlError key."""
    mock_db = _make_mock_db()
    mock_url_svc = AsyncMock()

    app = _build_legacy_test_app(
        {
            get_db: lambda: mock_db,
            get_url_service: lambda: mock_url_svc,
        }
    )
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        "/",
        data={},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 400
    body = resp.json()
    assert "UrlError" in body


def test_legacy_shorten_blocked_url():
    """POST / with blocked URL -> 403 with BlockedUrlError key."""
    mock_db = _make_mock_db()
    mock_url_svc = AsyncMock()

    app = _build_legacy_test_app(
        {
            get_db: lambda: mock_db,
            get_url_service: lambda: mock_url_svc,
        }
    )
    from dependencies import get_url_policy
    from services.safety.policy import PolicyRejection, UrlPolicyService

    class _BlockingGate(UrlPolicyService):
        async def check(self, url):
            return PolicyRejection(
                code="blocked_pattern", public_message="URL is blocked"
            )

    app.dependency_overrides[get_url_policy] = lambda: _BlockingGate(
        [], blocked_self_domains=[]
    )
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        "/",
        data={"url": "https://blocked.example.com"},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 403
    body = resp.json()
    assert "BlockedUrlError" in body


def test_legacy_shorten_invalid_alias():
    """POST / with invalid alias -> 400 with AliasError key."""
    mock_db = _make_mock_db()
    mock_url_svc = AsyncMock()

    app = _build_legacy_test_app(
        {
            get_db: lambda: mock_db,
            get_url_service: lambda: mock_url_svc,
        }
    )
    client = TestClient(app, raise_server_exceptions=False)

    with (
        patch("routes.legacy.url_shortener.validate_alias", return_value=False),
    ):
        resp = client.post(
            "/",
            data={"url": "https://example.com/long", "alias": "!!!invalid!!!"},
            headers={"Accept": "application/json"},
        )

    assert resp.status_code == 400
    body = resp.json()
    assert "AliasError" in body


def test_legacy_shorten_alias_exists():
    """POST / with existing alias -> 400 with AliasError key."""
    mock_db = _make_mock_db()
    mock_url_svc = AsyncMock()
    mock_url_svc.check_alias_available = AsyncMock(return_value=False)

    app = _build_legacy_test_app(
        {
            get_db: lambda: mock_db,
            get_url_service: lambda: mock_url_svc,
        }
    )
    client = TestClient(app, raise_server_exceptions=False)

    with (
        patch("routes.legacy.url_shortener.validate_alias", return_value=True),
    ):
        resp = client.post(
            "/",
            data={"url": "https://example.com/long", "alias": "taken1"},
            headers={"Accept": "application/json"},
        )

    assert resp.status_code == 400
    body = resp.json()
    assert "AliasError" in body
    assert "already exists" in body["AliasError"]


# ── POST /emoji ──────────────────────────────────────────────────────────────


def test_legacy_emoji_shorten_success():
    """POST /emoji with url -> JSON response with short_url."""
    mock_db = _make_mock_db()
    mock_url_svc = AsyncMock()
    mock_url_svc.check_alias_available = AsyncMock(return_value=True)

    app = _build_legacy_test_app(
        {
            get_db: lambda: mock_db,
            get_url_service: lambda: mock_url_svc,
        }
    )
    client = TestClient(app, raise_server_exceptions=False)

    with (
        patch(
            "routes.legacy.url_shortener.generate_emoji_alias",
            return_value="\U0001f600\U0001f680\U0001f389",
        ),
    ):
        resp = client.post(
            "/emoji",
            data={"url": "https://example.com/long"},
            headers={"Accept": "application/json"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert "short_url" in body
    assert "original_url" in body


def test_legacy_emoji_custom_alias_stored_canonical():
    """A VS16-variant custom alias is canonicalized before storage."""
    mock_db = _make_mock_db()
    mock_url_svc = AsyncMock()
    mock_url_svc.check_alias_available = AsyncMock(return_value=True)

    app = _build_legacy_test_app(
        {
            get_db: lambda: mock_db,
            get_url_service: lambda: mock_url_svc,
        }
    )
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        "/emoji",
        data={"url": "https://example.com/long", "emojies": "⭐️🎉"},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 200
    inserted = mock_db["emojis"].insert_one.call_args[0][0]
    assert inserted["_id"] == "⭐🎉"
    mock_url_svc.check_alias_available.assert_awaited_once_with("⭐🎉")


@pytest.mark.parametrize("alias", ["🇺🇸", "🏳️‍🌈", "1️⃣", "🎉" * 16])
def test_legacy_emoji_policy_rejected_returns_400(alias):
    """Formerly-lenient sequences (flags, ZWJ, keycaps, over-length) now 400
    with the frozen EmojiError shape."""
    mock_db = _make_mock_db()
    mock_url_svc = AsyncMock()
    mock_url_svc.check_alias_available = AsyncMock(return_value=True)

    app = _build_legacy_test_app(
        {
            get_db: lambda: mock_db,
            get_url_service: lambda: mock_url_svc,
        }
    )
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        "/emoji",
        data={"url": "https://example.com/long", "emojies": alias},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 400
    assert resp.json() == {"EmojiError": "Invalid emoji"}


def test_legacy_emoji_collision_with_v2_returns_400():
    """Cross-system uniqueness: an alias taken in urlsV2 must 400 here."""
    mock_db = _make_mock_db()
    mock_url_svc = AsyncMock()
    mock_url_svc.check_alias_available = AsyncMock(return_value=False)

    app = _build_legacy_test_app(
        {
            get_db: lambda: mock_db,
            get_url_service: lambda: mock_url_svc,
        }
    )
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        "/emoji",
        data={"url": "https://example.com/long", "emojies": "🚀🔥"},
        headers={"Accept": "application/json"},
    )

    assert resp.status_code == 400
    assert resp.json() == {"EmojiError": "Emoji already exists"}


def test_legacy_emoji_get_returns_400():
    """GET /emoji without url -> 400 JSON."""
    mock_db = _make_mock_db()
    mock_url_svc = AsyncMock()

    app = _build_legacy_test_app(
        {
            get_db: lambda: mock_db,
            get_url_service: lambda: mock_url_svc,
        }
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/emoji")

    assert resp.status_code == 400
    body = resp.json()
    assert "UrlError" in body


# ── GET /result/{code} ───────────────────────────────────────────────────────


def test_legacy_result_page_found():
    """GET /result/{code} for existing URL -> 200 HTML."""
    url_data = _make_cache_data(alias="abc123")
    mock_url_svc = AsyncMock()
    mock_url_svc.resolve = AsyncMock(return_value=(url_data, "v2"))

    app = _build_legacy_test_app(
        {
            get_url_service: lambda: mock_url_svc,
        }
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/result/abc123")

    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


def test_legacy_result_page_not_found():
    """GET /result/{code} for missing URL -> 404 HTML."""
    mock_url_svc = AsyncMock()
    mock_url_svc.resolve = AsyncMock(side_effect=NotFoundError("Not found"))

    app = _build_legacy_test_app(
        {
            get_url_service: lambda: mock_url_svc,
        }
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/result/nope42")

    assert resp.status_code == 404
    assert "text/html" in resp.headers.get("content-type", "")


# ── GET /metric ──────────────────────────────────────────────────────────────


def test_legacy_metric_endpoint():
    """GET /metric -> 200 JSON with expected keys."""
    mock_db = _make_mock_db()
    # Set up v1 metric aggregate
    v1_agg_cursor = MagicMock()
    v1_agg_cursor.to_list = AsyncMock(
        return_value=[{"total-shortlinks": 100, "total-clicks": 5000}]
    )
    mock_db["urls"].aggregate = AsyncMock(return_value=v1_agg_cursor)
    mock_db["urlsV2"].estimated_document_count = AsyncMock(return_value=50)
    mock_db["clicks"].estimated_document_count = AsyncMock(return_value=3000)

    mock_http_client = AsyncMock()
    mock_http_resp = MagicMock()
    mock_http_resp.status_code = 200
    mock_http_resp.json.return_value = {"stargazers_count": 42}
    mock_http_client.get = AsyncMock(return_value=mock_http_resp)

    @asynccontextmanager
    async def lifespan(app_inner: FastAPI):
        app_inner.state.settings = _SETTINGS
        app_inner.state.db = mock_db
        app_inner.state.redis = None
        app_inner.state.email_provider = MagicMock()
        app_inner.state.http_client = mock_http_client
        app_inner.state.oauth_providers = {}
        yield

    application = FastAPI(lifespan=lifespan)
    application.state.limiter = limiter
    application.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    register_error_handlers(application)
    if os.path.isdir(_STATIC_DIR):
        application.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    application.include_router(legacy_url_router)
    application.dependency_overrides[get_db] = lambda: mock_db
    application.dependency_overrides[get_redis] = lambda: None

    with TestClient(application, raise_server_exceptions=False) as client:
        resp = client.get("/metric")

    assert resp.status_code == 200
    body = resp.json()
    assert "total-shortlinks" in body
    assert "total-clicks" in body
    assert "github-stars" in body


# ── GET/POST /stats ──────────────────────────────────────────────────────────


def test_legacy_stats_form_get():
    """GET /stats -> 200 HTML form."""
    mock_db = _make_mock_db()

    app = _build_legacy_test_app(
        {
            get_db: lambda: mock_db,
        }
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/stats")

    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


def test_legacy_stats_post_not_found():
    """POST /stats with missing code -> renders stats.html with error (200)."""
    mock_db = _make_mock_db()

    app = _build_legacy_test_app(
        {
            get_db: lambda: mock_db,
        }
    )
    client = TestClient(app, raise_server_exceptions=False)

    with patch("routes.legacy.stats.LegacyUrlRepository") as MockLegacyRepo:
        mock_repo_instance = AsyncMock()
        mock_repo_instance.find_by_id = AsyncMock(return_value=None)
        MockLegacyRepo.return_value = mock_repo_instance

        resp = client.post(
            "/stats",
            data={"short_code": "nonexistent"},
        )

    # Legacy returns 200 with the stats form + error message
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


def test_legacy_stats_password_protected_redirect():
    """POST /stats for password-protected URL -> redirect to stats page with password."""
    mock_db = _make_mock_db()
    fake_doc = _FakeLegacyDoc(url="https://example.com", password="secret123")

    app = _build_legacy_test_app(
        {
            get_db: lambda: mock_db,
        }
    )
    client = TestClient(app, raise_server_exceptions=False)

    with patch("routes.legacy.stats.LegacyUrlRepository") as MockLegacyRepo:
        mock_repo_instance = AsyncMock()
        mock_repo_instance.find_by_id = AsyncMock(return_value=fake_doc)
        MockLegacyRepo.return_value = mock_repo_instance

        resp = client.post(
            "/stats",
            data={"short_code": "abc123", "password": "secret123"},
            follow_redirects=False,
        )

    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert "/stats/abc123" in location
    assert "password=secret123" in location


# ── GET /export/{code}/{format} ──────────────────────────────────────────────


def test_legacy_export_json():
    """POST /export/{code}/json with existing stats data -> 200 application/json."""
    stats_data = {
        "_id": "abc123",
        "url": "https://example.com",
        "total-clicks": 10,
        "counter": {"2025-01-01": 5, "2025-01-02": 5},
        "browser": {"Chrome": 8, "Firefox": 2},
        "country": {"US": 6, "UK": 4},
        "os_name": {"Windows": 7, "Mac": 3},
        "referrer": {"google.com": 5, "direct": 5},
        "unique_counter": {},
        "unique_browser": {},
        "unique_country": {},
        "unique_os_name": {},
        "unique_referrer": {},
        "bots": {},
        "creation-date": "2025-01-01",
        "creation-time": "12:00:00",
        "total_unique_clicks": 8,
        "password": None,
    }

    mock_db = _make_mock_db()

    app = _build_legacy_test_app(
        {
            get_db: lambda: mock_db,
        }
    )
    client = TestClient(app, raise_server_exceptions=False)

    with patch("routes.legacy.stats.LegacyUrlRepository") as MockLegacyRepo:
        mock_repo_instance = AsyncMock()
        mock_repo_instance.aggregate = AsyncMock(return_value=stats_data)
        MockLegacyRepo.return_value = mock_repo_instance

        with patch("routes.legacy.stats.is_emoji_alias", return_value=False):
            resp = client.post("/export/abc123/json")

    assert resp.status_code == 200
    assert "application/json" in resp.headers.get("content-type", "")


def test_legacy_export_invalid_format():
    """POST /export/{code}/pdf -> 400 with FormatError key."""
    mock_db = _make_mock_db()

    app = _build_legacy_test_app(
        {
            get_db: lambda: mock_db,
        }
    )
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post("/export/abc123/pdf")

    assert resp.status_code == 400
    body = resp.json()
    assert "FormatError" in body


def test_legacy_export_invalid_format_get():
    """GET /export/{code}/pdf -> 400 HTML error page."""
    mock_db = _make_mock_db()

    app = _build_legacy_test_app(
        {
            get_db: lambda: mock_db,
        }
    )
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/export/abc123/pdf")

    assert resp.status_code == 400
    assert "text/html" in resp.headers.get("content-type", "")
