"""Shared helpers for infrastructure tests."""

from unittest.mock import AsyncMock


def _url_data(**overrides):
    from app.infrastructure.cache.url_cache import UrlCacheData

    base = dict(
        id="507f1f77bcf86cd799439011",
        alias="abc1234",
        long_url="https://example.com",
        block_bots=False,
        password_hash=None,
        expiration_time=None,
        max_clicks=None,
        url_status="ACTIVE",
        schema_version="v2",
        owner_id="507f1f77bcf86cd799439012",
    )
    base.update(overrides)
    return UrlCacheData(**base)


def _fake_redis(get_returns=None):
    """Return a mock async Redis client."""
    r = AsyncMock()
    r.get.return_value = get_returns
    r.setex.return_value = True
    r.delete.return_value = 1
    r.set.return_value = True
    return r
