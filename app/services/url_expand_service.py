"""Redirect-chain expansion behind GET /api/v1/expand (the URL expander
tool). Follows the chain with the shared SSRF-guarded fetcher and
annotates it with the one safety signal we can honestly compute today:
whether any hop matches the abuse blocklist enforced at link creation."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.infrastructure.cache.meta_fetch_cache import MetaFetchCache
from app.infrastructure.cache.web_risk_budget import WebRiskBudget
from app.infrastructure.logging import get_logger
from app.infrastructure.safe_fetch import expand_public
from app.infrastructure.web_risk import WebRiskClient
from app.repositories.blocked_url_repository import BlockedUrlRepository
from app.shared.validators import validate_blocked_url

log = get_logger(__name__)


class UrlExpandService:
    def __init__(
        self,
        blocked_url_repo: BlockedUrlRepository,
        cache: MetaFetchCache,
        *,
        regex_timeout: float,
        user_agent: str,
        web_risk: WebRiskClient | None = None,
        web_risk_budget: WebRiskBudget | None = None,
    ) -> None:
        self._blocked_url_repo = blocked_url_repo
        self._cache = cache
        self._regex_timeout = regex_timeout
        self._user_agent = user_agent
        self._web_risk = web_risk
        self._web_risk_budget = web_risk_budget

    async def expand(self, url: str) -> dict:
        cached = await self._cache.get(url)
        if cached is not None:
            return cached

        chain = await expand_public(url, user_agent=self._user_agent)

        patterns = await self._blocked_url_repo.get_patterns()
        urls = [hop.url for hop in chain.hops] + [chain.final_url]
        blocklist_match, (web_risk, asked) = await asyncio.gather(
            asyncio.to_thread(self._any_blocked, urls, patterns),
            self._web_risk_verdict(chain.final_url),
        )

        payload = {
            "url": url,
            "final_url": chain.final_url,
            "final_status": chain.final_status,
            "truncated": chain.truncated,
            "hops": [
                {
                    "url": hop.url,
                    "status": hop.status,
                    "https": hop.url.startswith("https://"),
                }
                for hop in chain.hops
            ],
            "blocklist_match": blocklist_match,
            "web_risk": web_risk,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        # Caching an unanswered ask would hide the signal for the whole TTL;
        # a lookup we declined to make is a sustained state, so it caches.
        if not (asked and web_risk is None):
            await self._cache.set(url, payload)
        return payload

    async def _web_risk_verdict(self, url: str) -> tuple[dict | None, bool]:
        """Google Web Risk verdict for the final URL, and whether Google was
        actually asked. Absence is never a verdict."""
        if self._web_risk is None:
            return None, False
        if self._web_risk_budget is not None and not await self._web_risk_budget.take():
            return None, False
        threats = await self._web_risk.lookup(url)
        if threats is None:
            return None, True
        return {"checked": True, "threats": threats}, True

    def _any_blocked(self, urls: list[str], patterns: list[str]) -> bool:
        seen: set[str] = set()
        for url in urls:
            if url in seen:
                continue
            seen.add(url)
            if not validate_blocked_url(url, patterns, timeout=self._regex_timeout):
                return True
        return False
