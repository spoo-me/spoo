"""
Integration tests for cache hit/miss paths, stale fallback,
and Redis failure graceful degradation.

Tests that UrlCache and DualCache behave correctly through the
redirect and metric endpoints — cache hits skip DB, cache misses
fall through, and Redis errors are swallowed gracefully.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from bson import ObjectId
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/")

from config import AppSettings
from dependencies import (
    get_click_service,
    get_db,
    get_redis,
    get_settings,
    get_url_service,
)
from infrastructure.cache.url_cache import UrlCache, UrlCacheData
from middleware.error_handler import register_error_handlers
from middleware.rate_limiter import limiter
from routes.legacy.url_shortener import router as legacy_url_router
from routes.redirect_routes import router as redirect_router
from schemas.dto.requests.url import UpdateUrlRequest
from schemas.models.url import UrlV2Doc
from services.url_service import UrlService
from tests.conftest import build_test_app

# ── Helpers ──────────────────────────────────────────────────────────────────

_STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "static"
)


def _make_url_cache_data(
    alias: str = "abc1234",
    long_url: str = "https://example.com",
    schema: str = "v2",
    password_hash: str | None = None,
    block_bots: bool = False,
    max_clicks: int | None = None,
    total_clicks: int = 0,
    url_status: str = "ACTIVE",
) -> UrlCacheData:
    return UrlCacheData(
        id="507f1f77bcf86cd799439011",
        alias=alias,
        long_url=long_url,
        block_bots=block_bots,
        password_hash=password_hash,
        expiration_time=None,
        max_clicks=max_clicks,
        url_status=url_status,
        schema_version=schema,
        owner_id=None,
        total_clicks=total_clicks,
    )


def _make_url_service(
    cache: UrlCache,
    v2_doc_from_db: object | None = None,
) -> UrlService:
    """Build a real UrlService with mock repos but the given cache."""
    url_repo = AsyncMock()
    url_repo.find_by_alias = AsyncMock(return_value=v2_doc_from_db)
    url_repo.find_by_id = AsyncMock(return_value=v2_doc_from_db)
    url_repo.check_alias_exists = AsyncMock(return_value=False)
    url_repo.update = AsyncMock(return_value=None)
    url_repo.delete = AsyncMock(return_value=None)

    legacy_repo = AsyncMock()
    legacy_repo.find_by_id = AsyncMock(return_value=None)
    legacy_repo.check_exists = AsyncMock(return_value=False)

    emoji_repo = AsyncMock()
    emoji_repo.find_by_id = AsyncMock(return_value=None)

    blocked_url_repo = AsyncMock()
    blocked_url_repo.get_patterns = AsyncMock(return_value=[])

    from services.safety.policy import UrlPolicyService

    return UrlService(
        url_repo,
        legacy_repo,
        emoji_repo,
        blocked_url_repo,
        cache,
        [],
        system_default_domain="spoo.me",
        url_policy=UrlPolicyService([], blocked_self_domains=[]),
    )


def _mock_click_service():
    svc = MagicMock()
    svc.track_click = AsyncMock(return_value=None)
    return svc


def _make_v2_doc_mock(
    alias: str = "abc1234",
    long_url: str = "https://example.com",
    status: str = "ACTIVE",
):
    """Create a mock that quacks like UrlV2Doc for UrlCacheData.from_v2_doc."""
    now = datetime.now(timezone.utc)
    doc = MagicMock()
    doc.id = ObjectId("507f1f77bcf86cd799439011")
    doc.alias = alias
    doc.long_url = long_url
    doc.block_bots = False
    doc.password = None
    doc.expire_after = None
    doc.max_clicks = None
    doc.geo_rules = None
    doc.status = status
    doc.owner_id = ObjectId("000000000000000000000001")
    doc.total_clicks = 0
    doc.last_click = None
    doc.created_at = now
    doc.updated_at = None
    doc.private_stats = True
    doc.domain = "spoo.me"
    doc.meta_tags = None
    return doc


def _make_real_v2_doc(
    alias: str = "abc1234",
    long_url: str = "https://example.com",
    owner_id: ObjectId | None = None,
) -> UrlV2Doc:
    """Create a real UrlV2Doc instance for update/delete tests."""
    oid = owner_id or ObjectId("000000000000000000000001")
    return UrlV2Doc.from_mongo(
        {
            "_id": ObjectId("507f1f77bcf86cd799439011"),
            "alias": alias,
            "owner_id": oid,
            "created_at": datetime.now(timezone.utc),
            "long_url": long_url,
            "status": "ACTIVE",
            "total_clicks": 0,
            "domain": "spoo.me",
        }
    )


# ── Tests ────────────────────────────────────────────────────────────────────


def test_redirect_works_without_redis():
    """When app.state.redis is None, UrlCache returns None on get -> DB fallback works."""
    cache = UrlCache(redis_client=None)
    v2_doc = _make_v2_doc_mock()
    url_svc = _make_url_service(cache, v2_doc_from_db=v2_doc)
    click_svc = _mock_click_service()
    app = build_test_app(
        redirect_router,
        legacy_url_router,
        overrides={
            get_url_service: lambda: url_svc,
            get_click_service: lambda: click_svc,
        },
    )
    with TestClient(
        app, raise_server_exceptions=False, follow_redirects=False
    ) as client:
        resp = client.get("/abc1234")
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://example.com"


def test_redirect_works_when_cache_miss():
    """UrlCache.get returns None -> falls through to DB -> redirect works."""
    cache = AsyncMock(spec=UrlCache)
    cache.get = AsyncMock(return_value=None)
    cache.set = AsyncMock(return_value=None)
    cache.invalidate = AsyncMock(return_value=None)

    v2_doc = _make_v2_doc_mock()
    url_svc = _make_url_service(cache, v2_doc_from_db=v2_doc)
    click_svc = _mock_click_service()
    app = build_test_app(
        redirect_router,
        legacy_url_router,
        overrides={
            get_url_service: lambda: url_svc,
            get_click_service: lambda: click_svc,
        },
    )
    with TestClient(
        app, raise_server_exceptions=False, follow_redirects=False
    ) as client:
        resp = client.get("/abc1234")
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://example.com"
    cache.get.assert_called_once_with("abc1234", "spoo.me")


def test_redirect_populates_cache_on_miss():
    """After a cache miss + DB hit, url_cache.set is called to populate cache."""
    cache = AsyncMock(spec=UrlCache)
    cache.get = AsyncMock(return_value=None)
    cache.set = AsyncMock(return_value=None)
    cache.invalidate = AsyncMock(return_value=None)

    v2_doc = _make_v2_doc_mock()
    url_svc = _make_url_service(cache, v2_doc_from_db=v2_doc)
    click_svc = _mock_click_service()
    app = build_test_app(
        redirect_router,
        legacy_url_router,
        overrides={
            get_url_service: lambda: url_svc,
            get_click_service: lambda: click_svc,
        },
    )
    with TestClient(
        app, raise_server_exceptions=False, follow_redirects=False
    ) as client:
        resp = client.get("/abc1234")
    assert resp.status_code == 302
    cache.set.assert_called_once()
    call_args = cache.set.call_args
    assert call_args[0][0] == "abc1234"
    assert isinstance(call_args[0][1], UrlCacheData)
    assert call_args[0][1].domain == "spoo.me"


def test_redirect_uses_cached_data():
    """When cache.get returns UrlCacheData, no DB query is made and redirect is 302."""
    cached_data = _make_url_cache_data(long_url="https://cached-destination.com")
    cache = AsyncMock(spec=UrlCache)
    cache.get = AsyncMock(return_value=cached_data)
    cache.set = AsyncMock(return_value=None)
    cache.invalidate = AsyncMock(return_value=None)

    url_svc = _make_url_service(cache, v2_doc_from_db=None)
    click_svc = _mock_click_service()
    app = build_test_app(
        redirect_router,
        legacy_url_router,
        overrides={
            get_url_service: lambda: url_svc,
            get_click_service: lambda: click_svc,
        },
    )
    with TestClient(
        app, raise_server_exceptions=False, follow_redirects=False
    ) as client:
        resp = client.get("/abc1234")
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://cached-destination.com"
    url_svc._url_repo.find_by_alias.assert_not_called()
    url_svc._legacy_repo.find_by_id.assert_not_called()
    cache.set.assert_not_called()


def test_cache_invalidated_on_url_update():
    """After url_service.update(), cache.invalidate is called for the alias."""
    cache = AsyncMock(spec=UrlCache)
    cache.get = AsyncMock(return_value=None)
    cache.set = AsyncMock(return_value=None)
    cache.invalidate = AsyncMock(return_value=None)

    owner_id = ObjectId("000000000000000000000001")
    v2_doc = _make_real_v2_doc(owner_id=owner_id)

    url_svc = _make_url_service(cache, v2_doc_from_db=v2_doc)
    url_svc._url_repo.find_by_id = AsyncMock(return_value=v2_doc)

    loop = asyncio.new_event_loop()
    try:
        req = UpdateUrlRequest(long_url="https://new-dest.com")
        loop.run_until_complete(
            url_svc.update(ObjectId("507f1f77bcf86cd799439011"), req, owner_id)
        )
    finally:
        loop.close()

    cache.invalidate.assert_called_once_with("abc1234", "spoo.me")


def test_cache_invalidated_on_url_delete():
    """After url_service.delete(), cache.invalidate is called for the alias."""
    cache = AsyncMock(spec=UrlCache)
    cache.get = AsyncMock(return_value=None)
    cache.set = AsyncMock(return_value=None)
    cache.invalidate = AsyncMock(return_value=None)

    owner_id = ObjectId("000000000000000000000001")
    v2_doc = _make_real_v2_doc(owner_id=owner_id)

    url_svc = _make_url_service(cache, v2_doc_from_db=v2_doc)
    url_svc._url_repo.find_by_id = AsyncMock(return_value=v2_doc)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            url_svc.delete(ObjectId("507f1f77bcf86cd799439011"), owner_id)
        )
    finally:
        loop.close()

    cache.invalidate.assert_called_once_with("abc1234", "spoo.me")


def test_redis_error_on_get_falls_back_to_db():
    """With a real UrlCache whose underlying Redis raises, resolve still returns from DB."""
    broken_redis = AsyncMock()
    broken_redis.get = AsyncMock(side_effect=ConnectionError("Redis down"))
    broken_redis.setex = AsyncMock(side_effect=ConnectionError("Redis down"))

    cache = UrlCache(redis_client=broken_redis)
    v2_doc = _make_v2_doc_mock(long_url="https://fallback-dest.com")
    url_svc = _make_url_service(cache, v2_doc_from_db=v2_doc)
    click_svc = _mock_click_service()
    app = build_test_app(
        redirect_router,
        legacy_url_router,
        overrides={
            get_url_service: lambda: url_svc,
            get_click_service: lambda: click_svc,
        },
    )
    with TestClient(
        app, raise_server_exceptions=False, follow_redirects=False
    ) as client:
        resp = client.get("/abc1234")
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://fallback-dest.com"


def test_redis_error_on_set_does_not_crash():
    """When cache.set raises on populating cache, the redirect still works."""
    broken_redis = AsyncMock()
    broken_redis.get = AsyncMock(return_value=None)  # cache miss
    broken_redis.setex = AsyncMock(side_effect=ConnectionError("Redis down"))

    cache = UrlCache(redis_client=broken_redis)
    v2_doc = _make_v2_doc_mock(long_url="https://still-works.com")
    url_svc = _make_url_service(cache, v2_doc_from_db=v2_doc)
    click_svc = _mock_click_service()
    app = build_test_app(
        redirect_router,
        legacy_url_router,
        overrides={
            get_url_service: lambda: url_svc,
            get_click_service: lambda: click_svc,
        },
    )
    with TestClient(
        app, raise_server_exceptions=False, follow_redirects=False
    ) as client:
        resp = client.get("/abc1234")
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://still-works.com"


def test_metric_endpoint_works_without_redis():
    """GET /metric with redis=None falls through DualCache directly to query_fn."""
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

    http_client = MagicMock()
    gh_resp = MagicMock()
    gh_resp.status_code = 200
    gh_resp.json.return_value = {"stargazers_count": 42}
    http_client.get = AsyncMock(return_value=gh_resp)

    settings = AppSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.db = db
        app.state.redis = None
        app.state.email_provider = MagicMock()
        app.state.http_client = http_client
        app.state.oauth_providers = {}
        app.state.geoip = MagicMock()
        yield

    app = FastAPI(lifespan=lifespan)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    register_error_handlers(app)
    if os.path.isdir(_STATIC_DIR):
        app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    app.include_router(legacy_url_router)
    app.dependency_overrides.update(
        {get_db: lambda: db, get_redis: lambda: None, get_settings: lambda: settings}
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/metric")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total-shortlinks-raw"] == 150
    assert body["total-clicks-raw"] == 8000
    assert body["github-stars"] == 42
