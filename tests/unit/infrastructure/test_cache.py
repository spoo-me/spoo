"""Unit tests for UrlCache, UrlCacheData, and DualCache."""

import json
from unittest.mock import AsyncMock, patch

from bson import ObjectId

from app.infrastructure.cache.dual_cache import DualCache
from app.infrastructure.cache.url_cache import UrlCache, UrlCacheData

from .conftest import _fake_redis, _url_data

DOMAIN = "spoo.me"


class TestUrlCache:
    async def test_get_returns_none_when_redis_none(self):
        cache = UrlCache(redis_client=None)
        assert await cache.get("abc", DOMAIN) is None

    async def test_get_returns_data_on_hit(self):
        data = _url_data()
        r = _fake_redis(get_returns=json.dumps(data.__dict__))
        cache = UrlCache(r)
        result = await cache.get("abc1234", DOMAIN)
        assert result is not None
        assert result.long_url == "https://example.com"
        assert result.url_status == "ACTIVE"

    async def test_get_returns_none_on_miss(self):
        r = _fake_redis(get_returns=None)
        cache = UrlCache(r)
        assert await cache.get("missing", DOMAIN) is None

    async def test_get_decodes_legacy_payload_without_domain(self):
        # Pre-PR1 cached entries have no `domain` field. The model's empty
        # default keeps them decodable so a deploy doesn't 5xx until the
        # entire cache TTLs out.
        legacy_payload = {
            "_id": "507f1f77bcf86cd799439011",
            "alias": "abc1234",
            "long_url": "https://example.com",
            "block_bots": False,
            "password_hash": None,
            "expiration_time": None,
            "max_clicks": None,
            "url_status": "ACTIVE",
            "schema_version": "v2",
            "owner_id": "507f1f77bcf86cd799439012",
        }
        r = _fake_redis(get_returns=json.dumps(legacy_payload))
        cache = UrlCache(r)
        result = await cache.get("abc1234", DOMAIN)
        assert result is not None
        assert result.domain == ""

    async def test_set_calls_setex_with_ttl(self):
        r = _fake_redis()
        cache = UrlCache(r, ttl_seconds=300)
        await cache.set("abc1234", _url_data(domain=DOMAIN))
        r.setex.assert_called_once()
        call_args = r.setex.call_args[0]
        assert call_args[0] == f"url_cache:{DOMAIN}:abc1234"
        assert call_args[1] == 300

    async def test_set_noop_when_redis_none(self):
        cache = UrlCache(redis_client=None)
        await cache.set("abc", _url_data(domain=DOMAIN))  # must not raise

    async def test_invalidate_deletes_key(self):
        r = _fake_redis()
        cache = UrlCache(r)
        await cache.invalidate("abc1234", DOMAIN)
        r.delete.assert_called_once_with(f"url_cache:{DOMAIN}:abc1234")

    async def test_invalidate_noop_when_redis_none(self):
        cache = UrlCache(redis_client=None)
        await cache.invalidate("abc", DOMAIN)  # must not raise

    async def test_invalidate_many_deletes_all_keys(self):
        r = _fake_redis()
        cache = UrlCache(r)
        await cache.invalidate_many(["a", "b", "c"], DOMAIN)
        r.delete.assert_called_once_with(
            f"url_cache:{DOMAIN}:a",
            f"url_cache:{DOMAIN}:b",
            f"url_cache:{DOMAIN}:c",
        )

    async def test_invalidate_many_noop_on_empty_list(self):
        r = _fake_redis()
        cache = UrlCache(r)
        await cache.invalidate_many([], DOMAIN)
        r.delete.assert_not_called()

    async def test_invalidate_many_noop_when_redis_none(self):
        cache = UrlCache(redis_client=None)
        await cache.invalidate_many(["a", "b"], DOMAIN)  # must not raise

    async def test_set_stores_json_serialisable_data(self):
        r = _fake_redis()
        cache = UrlCache(r)
        await cache.set("x", _url_data(domain=DOMAIN, password_hash="$argon2id$..."))
        _, _, payload = r.setex.call_args[0]
        parsed = json.loads(payload)
        assert parsed["password_hash"] == "$argon2id$..."

    async def test_keys_scoped_per_domain(self):
        # Same alias, different domains → different cache slots.
        r = _fake_redis(get_returns=None)
        cache = UrlCache(r)
        await cache.get("sale", "spoo.me")
        await cache.get("sale", "links.acme.com")
        keys_used = [c.args[0] for c in r.get.call_args_list]
        assert keys_used == [
            "url_cache:spoo.me:sale",
            "url_cache:links.acme.com:sale",
        ]


class TestUrlCacheDataMetaFields:
    def test_decodes_payload_without_meta_fields(self):
        # Exact shape of a pre-meta-tags Redis entry: new fields must
        # default to None so a deploy doesn't 5xx until the cache TTLs out.
        from app.infrastructure.cache.url_cache import UrlCacheData

        legacy = (
            '{"_id":"507f1f77bcf86cd799439011","alias":"a","long_url":"https://x",'
            '"block_bots":false,"password_hash":null,"expiration_time":null,'
            '"max_clicks":null,"url_status":"ACTIVE","schema_version":"v2",'
            '"owner_id":null,"total_clicks":0,"domain":"spoo.me"}'
        )
        data = UrlCacheData.model_validate_json(legacy)
        assert data.meta_title is None
        assert data.meta_description is None
        assert data.meta_image is None
        assert data.meta_color is None
        assert data.meta_image_width is None
        assert data.meta_image_height is None
        assert data.expired_redirect_url is None

    def test_meta_fields_roundtrip_json(self):
        from app.infrastructure.cache.url_cache import UrlCacheData

        data = _url_data(
            meta_title="T", meta_image="https://x/i.png", meta_color="#112233"
        )
        restored = UrlCacheData.model_validate_json(data.model_dump_json(by_alias=True))
        assert restored.meta_title == "T"
        assert restored.meta_image == "https://x/i.png"
        assert restored.meta_color == "#112233"


class TestUrlCacheDataVerifyPassword:
    """Unit tests for UrlCacheData.verify_password()."""

    def test_no_password_returns_true_for_none(self):
        data = _url_data(password_hash=None)
        assert data.verify_password(None) is True

    def test_no_password_returns_true_for_any_input(self):
        data = _url_data(password_hash=None)
        assert data.verify_password("anything") is True

    def test_v2_correct_password(self):
        data = _url_data(password_hash="$argon2id$hash", schema_version="v2")
        with patch(
            "app.infrastructure.cache.url_cache.verify_password_hash", return_value=True
        ):
            assert data.verify_password("correct") is True

    def test_v2_wrong_password(self):
        data = _url_data(password_hash="$argon2id$hash", schema_version="v2")
        with patch(
            "app.infrastructure.cache.url_cache.verify_password_hash",
            return_value=False,
        ):
            assert data.verify_password("wrong") is False

    def test_v2_none_password_short_circuits_without_hashing(self):
        data = _url_data(password_hash="$argon2id$hash", schema_version="v2")
        with patch("app.infrastructure.cache.url_cache.verify_password_hash") as mock:
            result = data.verify_password(None)
            assert result is False
            mock.assert_not_called()

    def test_v1_plaintext_correct(self):
        data = _url_data(password_hash="secret123", schema_version="v1")
        assert data.verify_password("secret123") is True

    def test_v1_plaintext_wrong(self):
        data = _url_data(password_hash="secret123", schema_version="v1")
        assert data.verify_password("wrong") is False

    def test_emoji_plaintext_correct(self):
        data = _url_data(password_hash="mypass", schema_version="emoji")
        assert data.verify_password("mypass") is True

    def test_emoji_plaintext_wrong(self):
        data = _url_data(password_hash="mypass", schema_version="emoji")
        assert data.verify_password("nope") is False

    def test_v1_none_password_does_not_match(self):
        data = _url_data(password_hash="secret", schema_version="v1")
        assert data.verify_password(None) is False


class TestDualCache:
    async def test_returns_live_data_on_primary_hit(self):
        r = AsyncMock()
        r.get = AsyncMock(side_effect=[json.dumps({"v": 1}), None])
        r.set = AsyncMock(return_value=True)
        cache = DualCache(r)
        result = await cache.get_or_set("key", AsyncMock(return_value={"v": 99}))
        assert result == {"v": 1}

    async def test_returns_stale_and_schedules_refresh(self):
        r = AsyncMock()
        # primary miss, stale hit
        r.get = AsyncMock(side_effect=[None, json.dumps({"v": "stale"})])
        r.set = AsyncMock(return_value=True)
        cache = DualCache(r)
        result = await cache.get_or_set("key", AsyncMock(return_value={"v": "fresh"}))
        assert result == {"v": "stale"}

    async def test_calls_query_fn_on_full_miss(self):
        r = AsyncMock()
        r.get = AsyncMock(return_value=None)
        r.set = AsyncMock(return_value=True)  # lock acquired
        r.setex = AsyncMock()
        r.delete = AsyncMock()
        query = AsyncMock(return_value={"v": "fresh"})
        cache = DualCache(r)
        result = await cache.get_or_set("key", query)
        assert result == {"v": "fresh"}
        query.assert_awaited_once()

    async def test_returns_none_when_redis_none(self):
        called = False

        async def query():
            nonlocal called
            called = True
            return {"v": 1}

        cache = DualCache(redis_client=None)
        result = await cache.get_or_set("key", query)
        # When redis is None, query is called directly
        assert called
        assert result == {"v": 1}

    async def test_returns_none_on_lock_contention(self):
        r = AsyncMock()
        r.get = AsyncMock(return_value=None)  # both miss
        r.set = AsyncMock(return_value=None)  # lock NOT acquired
        cache = DualCache(r)
        result = await cache.get_or_set("key", AsyncMock(return_value={"v": 1}))
        assert result is None


class TestUrlCacheDataGeoRules:
    async def test_pre_geo_payload_decodes_with_none(self):
        """Entries cached before geo_rules existed must stay decodable —
        the field defaults to None, so no cache version bump is needed."""
        payload = {
            "_id": "507f1f77bcf86cd799439011",
            "alias": "abc1234",
            "long_url": "https://example.com",
            "block_bots": False,
            "password_hash": None,
            "expiration_time": None,
            "max_clicks": None,
            "url_status": "ACTIVE",
            "schema_version": "v2",
            "owner_id": "507f1f77bcf86cd799439012",
            "domain": DOMAIN,
        }
        r = _fake_redis(get_returns=json.dumps(payload))
        cache = UrlCache(r)
        result = await cache.get("abc1234", DOMAIN)
        assert result is not None
        assert result.geo_rules is None

    async def test_geo_rules_round_trip(self):
        rules = {"IN": "https://example.in/", "US": "https://example.com/us"}
        data = _url_data(domain=DOMAIN)
        data = data.model_copy(update={"geo_rules": rules})
        r = _fake_redis()
        cache = UrlCache(r)
        await cache.set("abc1234", data)
        stored_json = r.setex.call_args[0][2]

        r2 = _fake_redis(get_returns=stored_json)
        cache2 = UrlCache(r2)
        result = await cache2.get("abc1234", DOMAIN)
        assert result is not None
        assert result.geo_rules == rules


class TestUrlCacheDataAbVariants:
    async def test_pre_variants_payload_decodes_with_none(self):
        payload = {
            "_id": "507f1f77bcf86cd799439011",
            "alias": "abc1234",
            "long_url": "https://example.com",
            "block_bots": False,
            "password_hash": None,
            "expiration_time": None,
            "max_clicks": None,
            "url_status": "ACTIVE",
            "schema_version": "v2",
            "owner_id": "507f1f77bcf86cd799439012",
            "domain": DOMAIN,
        }
        r = _fake_redis(get_returns=json.dumps(payload))
        cache = UrlCache(r)
        result = await cache.get("abc1234", DOMAIN)
        assert result is not None
        assert result.ab_variants is None

    async def test_ab_variants_round_trip(self):
        from app.schemas.models.url import AbVariant

        variants = [AbVariant(url="https://example.com/b", weight=40)]
        data = _url_data(domain=DOMAIN).model_copy(update={"ab_variants": variants})
        r = _fake_redis()
        cache = UrlCache(r)
        await cache.set("abc1234", data)
        stored_json = r.setex.call_args[0][2]

        r2 = _fake_redis(get_returns=stored_json)
        result = await UrlCache(r2).get("abc1234", DOMAIN)
        assert result is not None
        assert result.ab_variants == variants


class TestUrlCacheDataIsTimeExpired:
    def _data(self, expiration_time):
        return UrlCacheData(
            id="x",
            alias="abc1234",
            long_url="https://example.com",
            block_bots=False,
            password_hash=None,
            expiration_time=expiration_time,
            max_clicks=None,
            url_status="ACTIVE",
            schema_version="v2",
            owner_id=None,
        )

    def test_none_never_expires(self):
        assert self._data(None).is_time_expired(1_800_000_000) is False

    def test_past_is_expired(self):
        assert self._data(1_000).is_time_expired(2_000) is True

    def test_boundary_equal_is_expired(self):
        # <= convention, shared with UrlV2Doc.effective_status
        assert self._data(2_000).is_time_expired(2_000) is True

    def test_future_is_not_expired(self):
        assert self._data(3_000).is_time_expired(2_000) is False


class TestFromV2DocExpirationNormalization:
    def test_naive_expire_after_treated_as_utc(self):
        """Mongo returns naive UTC — the projection must not interpret it
        in host-local time."""
        from datetime import datetime, timezone

        from app.schemas.models.url import UrlV2Doc

        naive = datetime(2025, 6, 1, 12, 0, 0)  # naive UTC from Mongo
        doc = UrlV2Doc.from_mongo(
            {
                "_id": ObjectId(),
                "alias": "abc1234",
                "domain": "spoo.me",
                "created_at": naive,
                "long_url": "https://example.com",
                "expire_after": naive,
            }
        )
        data = UrlCacheData.from_v2_doc(doc)
        expected = int(naive.replace(tzinfo=timezone.utc).timestamp())
        assert data.expiration_time == expected


class TestUrlCacheDataIsNotYetLive:
    def _data(self, start_time, pre_start_url=None):
        return UrlCacheData(
            id="x",
            alias="abc1234",
            long_url="https://example.com",
            block_bots=False,
            password_hash=None,
            expiration_time=None,
            max_clicks=None,
            url_status="ACTIVE",
            schema_version="v2",
            owner_id=None,
            start_time=start_time,
            pre_start_url=pre_start_url,
        )

    def test_none_is_live(self):
        assert self._data(None).is_not_yet_live(1_800_000_000) is False

    def test_future_is_not_yet_live(self):
        assert self._data(3_000).is_not_yet_live(2_000) is True

    def test_boundary_equal_is_live(self):
        # Strict > : the link goes live at the exact second.
        assert self._data(2_000).is_not_yet_live(2_000) is False

    def test_past_is_live(self):
        assert self._data(1_000).is_not_yet_live(2_000) is False

    def test_entries_written_before_the_field_deserialize_live(self):
        """Redis entries from before scheduling shipped carry no start_time;
        they must read as live, not fail validation."""
        raw = (
            '{"_id":"x","alias":"abc1234","long_url":"https://example.com",'
            '"block_bots":false,"password_hash":null,"expiration_time":null,'
            '"max_clicks":null,"url_status":"ACTIVE","schema_version":"v2",'
            '"owner_id":null}'
        )
        data = UrlCacheData.model_validate_json(raw)
        assert data.start_time is None
        assert data.pre_start_url is None
        assert data.is_not_yet_live(1_800_000_000) is False

    def test_from_v2_doc_carries_start_and_fallback(self):
        from datetime import datetime, timezone

        from app.schemas.models.url import UrlV2Doc

        starts = datetime(2030, 6, 1, 12, 0, 0)  # naive UTC from Mongo
        doc = UrlV2Doc.from_mongo(
            {
                "_id": ObjectId(),
                "alias": "abc1234",
                "domain": "spoo.me",
                "created_at": starts,
                "long_url": "https://example.com",
                "starts_at": starts,
                "pre_start_url": "https://example.org/soon",
            }
        )
        data = UrlCacheData.from_v2_doc(doc)
        assert data.start_time == int(starts.replace(tzinfo=timezone.utc).timestamp())
        assert data.pre_start_url == "https://example.org/soon"


class TestFromV2DocFractionalStart:
    def test_fractional_start_rounds_up_so_the_link_is_never_live_early(self):
        from datetime import datetime, timezone

        from app.schemas.models.url import UrlV2Doc

        starts = datetime(2030, 6, 1, 12, 0, 0, 900_000, tzinfo=timezone.utc)
        doc = UrlV2Doc.from_mongo(
            {
                "_id": ObjectId(),
                "alias": "abc1234",
                "domain": "spoo.me",
                "created_at": starts,
                "long_url": "https://example.com",
                "starts_at": starts,
            }
        )
        data = UrlCacheData.from_v2_doc(doc)
        whole = int(starts.replace(microsecond=0).timestamp())
        assert data.start_time == whole + 1
        # At the floor second the link is still not live; one second on it is.
        assert data.is_not_yet_live(whole) is True
        assert data.is_not_yet_live(whole + 1) is False
