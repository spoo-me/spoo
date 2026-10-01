"""Lapse policies on the redirect path, through the real route and a real
UrlService: a lapsed owner's link behaves like a free link, the versioned
cache entry is reshaped once, and a store failure never breaks a redirect."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from bson import ObjectId
from fastapi.testclient import TestClient

from dependencies import get_click_sink, get_entitlement_service, get_url_service
from infrastructure.cache.url_cache import UrlCache, UrlCacheData
from routes.redirect_routes import router as redirect_router
from schemas.models.url import UrlV2Doc
from services.entitlements import Resolved, for_plan
from services.features.catalog import Plan
from services.safety.policy import UrlPolicyService
from services.url_service import UrlService
from tests.conftest import build_test_app

OWNER = ObjectId("aaaaaaaaaaaaaaaaaaaaaaaa")
GEO_RULES = {"IN": "https://example.in/", "US": "https://example.com/us"}
BROWSER_UA = "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
BOT_UA = "Twitterbot/1.0"


class _FakeRedis:
    """Just enough of redis.asyncio for UrlCache: get / setex / delete."""

    def __init__(self):
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        self.store[key] = value

    async def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)


def _doc(**overrides) -> UrlV2Doc:
    base = {
        "_id": ObjectId("507f1f77bcf86cd799439011"),
        "alias": "abc1234",
        "owner_id": OWNER,
        "created_at": datetime.now(timezone.utc),
        "long_url": "https://example.com/default",
        "status": "ACTIVE",
        "total_clicks": 0,
        "domain": "spoo.me",
        "geo_rules": GEO_RULES,
    }
    base.update(overrides)
    return UrlV2Doc.from_mongo(base)


def _url_service(cache: UrlCache, doc: UrlV2Doc | None, entitlements) -> UrlService:
    url_repo = AsyncMock()
    url_repo.find_by_alias = AsyncMock(return_value=doc)
    url_repo.find_by_id = AsyncMock(return_value=doc)
    url_repo.expire_if_time_reached = AsyncMock(return_value=False)
    legacy_repo = AsyncMock()
    legacy_repo.find_by_id = AsyncMock(return_value=None)
    emoji_repo = AsyncMock()
    emoji_repo.find_by_id = AsyncMock(return_value=None)
    blocked = AsyncMock()
    blocked.get_patterns = AsyncMock(return_value=[])
    svc = UrlService(
        url_repo,
        legacy_repo,
        emoji_repo,
        blocked,
        cache,
        [],
        system_default_domain="spoo.me",
        url_policy=UrlPolicyService([], blocked_self_domains=[]),
        entitlements=entitlements,
    )
    return svc


def _entitlements(resolved: Resolved):
    svc = AsyncMock()
    svc.resolve_for = AsyncMock(return_value=resolved)
    return svc


def _client(url_svc, ent_svc) -> TestClient:
    sink = MagicMock()
    sink.emit = AsyncMock(return_value=None)
    app = build_test_app(
        redirect_router,
        overrides={
            get_url_service: lambda: url_svc,
            get_click_sink: lambda: sink,
            get_entitlement_service: lambda: ent_svc,
        },
    )
    return TestClient(app, raise_server_exceptions=False)


def _get(client, path="/abc1234", ua=BROWSER_UA, country="IN"):
    return client.get(
        path,
        headers={"User-Agent": ua, "CF-IPCountry": country},
        follow_redirects=False,
    )


PRO = for_plan(Plan.PRO, version=1)
LAPSED = Resolved(
    plan=Plan.FREE,
    status="lapsed",
    values=for_plan(Plan.FREE).values,
    version=2,
)


class TestGeoRules:
    def test_pro_owner_gets_the_country_rule(self):
        ent = _entitlements(PRO)
        client = _client(_url_service(UrlCache(None), _doc(), ent), ent)
        resp = _get(client)
        assert resp.status_code == 302
        assert resp.headers["location"] == "https://example.in/"

    def test_lapsed_owner_falls_back_to_long_url(self):
        ent = _entitlements(LAPSED)
        client = _client(_url_service(UrlCache(None), _doc(), ent), ent)
        resp = _get(client)
        assert resp.status_code == 302
        assert resp.headers["location"] == "https://example.com/default"


AB_VARIANTS = [{"url": "https://example.com/b", "weight": 100}]


class TestAbVariants:
    def test_pro_owner_is_split_to_the_variant(self):
        ent = _entitlements(PRO)
        doc = _doc(geo_rules=None, ab_variants=AB_VARIANTS)
        client = _client(_url_service(UrlCache(None), doc, ent), ent)
        resp = _get(client)
        assert resp.status_code == 302
        assert resp.headers["location"] == "https://example.com/b"

    def test_lapsed_owner_always_gets_the_default(self):
        ent = _entitlements(LAPSED)
        doc = _doc(geo_rules=None, ab_variants=AB_VARIANTS)
        client = _client(_url_service(UrlCache(None), doc, ent), ent)
        resp = _get(client)
        assert resp.status_code == 302
        assert resp.headers["location"] == "https://example.com/default"


class TestExpiredFallback:
    def _expired(self):
        return _doc(
            geo_rules=None,
            status="EXPIRED",
            expire_after=datetime.now(timezone.utc) - timedelta(days=1),
            expired_redirect_url="https://example.com/after",
        )

    def test_pro_owner_is_sent_to_the_fallback(self):
        ent = _entitlements(PRO)
        client = _client(_url_service(UrlCache(None), self._expired(), ent), ent)
        resp = _get(client)
        assert resp.status_code == 302
        assert resp.headers["location"] == "https://example.com/after"

    def test_lapsed_owner_sees_the_expired_page(self):
        ent = _entitlements(LAPSED)
        client = _client(_url_service(UrlCache(None), self._expired(), ent), ent)
        resp = _get(client)
        assert resp.status_code == 410


class TestMetaCard:
    def _with_meta(self):
        return _doc(
            geo_rules=None,
            meta_tags={"title": "Owner card", "description": "d"},
        )

    def test_pro_owner_serves_the_card_to_bots(self):
        ent = _entitlements(PRO)
        client = _client(_url_service(UrlCache(None), self._with_meta(), ent), ent)
        resp = _get(client, ua=BOT_UA)
        assert resp.status_code == 200
        assert "Owner card" in resp.text

    def test_lapsed_owner_gives_bots_the_plain_redirect(self):
        ent = _entitlements(LAPSED)
        client = _client(_url_service(UrlCache(None), self._with_meta(), ent), ent)
        resp = _get(client, ua=BOT_UA)
        assert resp.status_code == 302
        assert resp.headers["location"] == "https://example.com/default"


class TestVersionedCache:
    def test_stale_entry_is_reshaped_once_and_then_served_from_cache(self):
        redis = _FakeRedis()
        cache = UrlCache(redis)
        ent = _entitlements(PRO)
        svc = _url_service(cache, _doc(), ent)
        client = _client(svc, ent)

        # First hit: miss, shaped under version 1 with the geo rules kept.
        assert _get(client).headers["location"] == "https://example.in/"
        entry = UrlCacheData.model_validate_json(
            redis.store["url_cache:spoo.me:abc1234"]
        )
        assert entry.owner_ent_version == 1
        assert entry.geo_rules == GEO_RULES
        assert svc._url_repo.find_by_alias.await_count == 1

        # Owner lapses: version 2. The cached entry is stale, so the document
        # is re-read once and the entry rewritten without the rules.
        ent.resolve_for = AsyncMock(return_value=LAPSED)
        assert _get(client).headers["location"] == "https://example.com/default"
        entry = UrlCacheData.model_validate_json(
            redis.store["url_cache:spoo.me:abc1234"]
        )
        assert entry.owner_ent_version == 2
        assert entry.geo_rules is None
        assert svc._url_repo.find_by_alias.await_count == 2

        # Same version again: served straight from the cache.
        assert _get(client).headers["location"] == "https://example.com/default"
        assert svc._url_repo.find_by_alias.await_count == 2

    def test_resubscribe_brings_the_rules_back(self):
        redis = _FakeRedis()
        cache = UrlCache(redis)
        ent = _entitlements(LAPSED)
        svc = _url_service(cache, _doc(), ent)
        client = _client(svc, ent)
        assert _get(client).headers["location"] == "https://example.com/default"
        ent.resolve_for = AsyncMock(return_value=for_plan(Plan.PRO, version=3))
        assert _get(client).headers["location"] == "https://example.in/"


class TestDegradation:
    def test_redirect_survives_redis_down(self):
        # UrlCache(None) is exactly the no-Redis state: every get misses and
        # every set is a no-op, so the redirect comes straight from Mongo.
        ent = _entitlements(PRO)
        client = _client(_url_service(UrlCache(None), _doc(), ent), ent)
        resp = _get(client, country="US")
        assert resp.status_code == 302
        assert resp.headers["location"] == "https://example.com/us"

    def test_degraded_resolver_serves_the_cached_behaviour_as_is(self):
        redis = _FakeRedis()
        cache = UrlCache(redis)
        ent = _entitlements(PRO)
        svc = _url_service(cache, _doc(), ent)
        client = _client(svc, ent)
        assert _get(client).headers["location"] == "https://example.in/"

        degraded = for_plan(Plan.FREE).model_copy(update={"degraded": True})
        ent.resolve_for = AsyncMock(return_value=degraded)
        resp = _get(client)
        assert resp.status_code == 302
        assert resp.headers["location"] == "https://example.in/"
        assert svc._url_repo.find_by_alias.await_count == 1

    def test_anonymous_link_never_consults_the_resolver(self):
        ent = _entitlements(LAPSED)
        svc = _url_service(UrlCache(None), _doc(owner_id=None), ent)
        client = _client(svc, ent)
        assert _get(client).headers["location"] == "https://example.in/"
        ent.resolve_for.assert_not_awaited()
