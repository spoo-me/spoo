"""Expander Web Risk budget — infrastructure/cache/web_risk_budget.py."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.infrastructure.cache.web_risk_budget import WebRiskBudget


def _redis(counts):
    redis = AsyncMock()
    redis.incr = AsyncMock(side_effect=counts)
    redis.expire = AsyncMock()
    return redis


@pytest.mark.asyncio
async def test_calls_within_the_cap_are_granted():
    budget = WebRiskBudget(_redis([1, 2, 3]), limit=3)
    assert [await budget.take() for _ in range(3)] == [True, True, True]


@pytest.mark.asyncio
async def test_the_call_past_the_cap_is_refused():
    budget = WebRiskBudget(_redis([3, 4]), limit=3)
    assert await budget.take() is True
    assert await budget.take() is False


@pytest.mark.asyncio
async def test_the_daily_key_expires_so_it_self_clears():
    redis = _redis([1])
    await WebRiskBudget(redis, limit=5).take()
    key, ttl = redis.expire.await_args[0]
    assert key.startswith("web_risk_budget:")
    assert ttl > 86_400

    # Only the call that created the key sets the TTL.
    redis = _redis([2])
    await WebRiskBudget(redis, limit=5).take()
    redis.expire.assert_not_awaited()


@pytest.mark.asyncio
async def test_without_redis_nothing_is_spent():
    assert await WebRiskBudget(None, limit=1_000).take() is False


@pytest.mark.asyncio
async def test_a_broken_counter_refuses_rather_than_waving_calls_through():
    """The counter shares its Redis with the expander's result cache, so an
    outage drops the cache and the cap together. Unable to count means
    unable to spend, or the quota goes exactly when it is least affordable.
    """
    redis = AsyncMock()
    redis.incr = AsyncMock(side_effect=RuntimeError("redis down"))
    assert await WebRiskBudget(redis, limit=1_000).take() is False
