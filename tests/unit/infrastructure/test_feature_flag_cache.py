"""Unit tests for FeatureFlagCache.

Mirrors the shape of ``test_cache.py::TestUrlCache``: hit/miss/no-redis
tolerance, plus the negative-sentinel and JSON-decode-error paths that are
specific to this cache.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

from app.infrastructure.cache.feature_flag_cache import (
    NEGATIVE_MISS,
    FeatureFlagCache,
    NegativeMiss,
)
from app.schemas.enums.rollout_type import RolloutType
from app.schemas.models.feature_flag import FeatureFlagDoc

from .conftest import _fake_redis


def _flag(**overrides) -> FeatureFlagDoc:
    base = {
        "name": "test_flag",
        "enabled": True,
        "rollout_type": RolloutType.EVERYONE,
        "allowlist_user_ids": [],
        "allowlist_emails": [],
        "percentage": 0,
        "enabled_digits": [],
        "tier": None,
        "description": "",
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    base.update(overrides)
    return FeatureFlagDoc.model_validate(base)


class TestFeatureFlagCacheGet:
    async def test_get_returns_none_when_redis_none(self):
        cache = FeatureFlagCache(redis_client=None)
        assert await cache.get("any") is None

    async def test_get_returns_doc_on_hit(self):
        flag = _flag()
        r = _fake_redis(get_returns=flag.model_dump_json(by_alias=True))
        cache = FeatureFlagCache(r)
        result = await cache.get("test_flag")
        assert isinstance(result, FeatureFlagDoc)
        assert result.name == "test_flag"
        assert result.enabled is True

    async def test_get_returns_none_on_miss(self):
        r = _fake_redis(get_returns=None)
        cache = FeatureFlagCache(r)
        assert await cache.get("missing") is None

    async def test_get_returns_negative_miss_on_str_sentinel(self):
        # Service set_negative writes the sentinel as a string. Confirm the
        # cache decodes it back into the singleton ``NEGATIVE_MISS``.
        r = _fake_redis(get_returns="MISS")
        cache = FeatureFlagCache(r)
        result = await cache.get("missing")
        assert result is NEGATIVE_MISS
        assert isinstance(result, NegativeMiss)

    async def test_get_returns_negative_miss_on_bytes_sentinel(self):
        # Redis client may return bytes when decode_responses=False.
        r = _fake_redis(get_returns=b"MISS")
        cache = FeatureFlagCache(r)
        result = await cache.get("missing")
        assert result is NEGATIVE_MISS

    async def test_get_returns_none_on_decode_error(self):
        # Corrupted cache entry — must not raise; service falls through to repo.
        r = _fake_redis(get_returns='{"this is": not_valid_json"')
        cache = FeatureFlagCache(r)
        assert await cache.get("test_flag") is None

    async def test_get_swallows_redis_error(self):
        r = AsyncMock()
        r.get.side_effect = ConnectionError("redis down")
        cache = FeatureFlagCache(r)
        # Swallows the exception — service falls through to repo.
        assert await cache.get("test_flag") is None


class TestFeatureFlagCacheSet:
    async def test_set_calls_setex_with_ttl(self):
        r = _fake_redis()
        cache = FeatureFlagCache(r, ttl_seconds=42)
        await cache.set("test_flag", _flag())
        r.setex.assert_called_once()
        call_args = r.setex.call_args[0]
        assert call_args[0] == "flag:test_flag"
        assert call_args[1] == 42

    async def test_set_stores_json_serialisable_doc(self):
        import json

        r = _fake_redis()
        cache = FeatureFlagCache(r)
        await cache.set("test_flag", _flag(percentage=42))
        _, _, payload = r.setex.call_args[0]
        parsed = json.loads(payload)
        assert parsed["name"] == "test_flag"
        assert parsed["percentage"] == 42

    async def test_set_noop_when_redis_none(self):
        cache = FeatureFlagCache(redis_client=None)
        await cache.set("test_flag", _flag())  # must not raise

    async def test_set_swallows_redis_error(self):
        r = AsyncMock()
        r.setex.side_effect = ConnectionError("redis down")
        cache = FeatureFlagCache(r)
        # Swallows — caller doesn't need to retry.
        await cache.set("test_flag", _flag())


class TestFeatureFlagCacheSetNegative:
    async def test_set_negative_calls_setex_with_negative_ttl(self):
        r = _fake_redis()
        cache = FeatureFlagCache(r, negative_ttl_seconds=15)
        await cache.set_negative("missing")
        r.setex.assert_called_once()
        call_args = r.setex.call_args[0]
        assert call_args[0] == "flag:missing"
        assert call_args[1] == 15
        assert call_args[2] == "MISS"

    async def test_set_negative_noop_when_redis_none(self):
        cache = FeatureFlagCache(redis_client=None)
        await cache.set_negative("missing")  # must not raise

    async def test_set_negative_swallows_redis_error(self):
        r = AsyncMock()
        r.setex.side_effect = ConnectionError("redis down")
        cache = FeatureFlagCache(r)
        await cache.set_negative("missing")


class TestFeatureFlagCacheInvalidate:
    async def test_invalidate_deletes_key(self):
        r = _fake_redis()
        cache = FeatureFlagCache(r)
        await cache.invalidate("test_flag")
        r.delete.assert_called_once_with("flag:test_flag")

    async def test_invalidate_noop_when_redis_none(self):
        cache = FeatureFlagCache(redis_client=None)
        await cache.invalidate("test_flag")  # must not raise

    async def test_invalidate_swallows_redis_error(self):
        r = AsyncMock()
        r.delete.side_effect = ConnectionError("redis down")
        cache = FeatureFlagCache(r)
        await cache.invalidate("test_flag")


class TestFeatureFlagCacheKeyPrefix:
    """Keys must use the ``flag:`` prefix so they don't collide with
    ``url_cache:`` or any other Redis namespace in the same instance."""

    def test_key_prefix(self):
        cache = FeatureFlagCache(redis_client=None)
        assert cache._key("custom_domains") == "flag:custom_domains"
