"""Daily cap on the URL expander's share of the Web Risk quota.

The expander and the safety analyzer run on one project's quota, but only
the expander is a public endpoint. Without a cap, traffic on the tool can
spend the free tier and leave abuse analysis with no online reputation
source for the rest of the day, looking exactly like a transient error.
So the expander yields first: past its cap it stops asking and its verdict
is simply absent, which is a state the tool already renders.

Global, not per caller. Per-caller rate limits bound one IP; this bounds
the bill and the analyzer's headroom.

Unlike the caches here, it fails closed. It shares its Redis with the
expander's result cache, so an outage removes the cache that absorbs
lookups and the cap that bounds them at the same moment. Unable to count
means unable to spend.
"""

from __future__ import annotations

from datetime import datetime, timezone

import redis.asyncio as aioredis

from app.infrastructure.logging import get_logger

log = get_logger(__name__)

_KEY_TTL_SECONDS = 172_800


class WebRiskBudget:
    def __init__(
        self,
        redis_client: aioredis.Redis | None,
        *,
        limit: int,
        prefix: str = "web_risk_budget",
    ) -> None:
        self._redis = redis_client
        self._limit = limit
        self._prefix = prefix

    async def take(self) -> bool:
        """Claim one lookup. False once the day's cap is spent, and false
        whenever the count is unavailable."""
        if self._redis is None:
            return False
        key = f"{self._prefix}:{datetime.now(timezone.utc):%Y-%m-%d}"
        try:
            used = await self._redis.incr(key)
            if used == 1:
                await self._redis.expire(key, _KEY_TTL_SECONDS)
        except Exception as exc:
            log.warning("web_risk_budget_error", error=str(exc))
            return False
        if used == self._limit + 1:
            log.warning("web_risk_budget_exhausted", limit=self._limit)
        return used <= self._limit
