"""ClickHandler protocol and ClickContext dataclass."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from typing_extensions import Protocol

from app.infrastructure.cache.url_cache import UrlCacheData


@dataclass
class ClickContext:
    """All metadata available at click time, passed to the active handler."""

    url_data: UrlCacheData
    short_code: str
    client_ip: str
    redirect_ms: int
    user_agent: str
    referrer: str | None
    # When the visitor clicked, not when a worker got round to it.
    clicked_at: datetime
    is_emoji: bool = False
    cf_city: str | None = None
    # Campaign tags from the short link's query string (already sanitised
    # by ClickEvent). Recorded by the v2 handler; legacy ignores them.
    utm_source: str | None = None
    utm_medium: str | None = None
    utm_campaign: str | None = None
    variant_index: int | None = None


class ClickHandler(Protocol):
    """Minimal protocol every click handler must satisfy."""

    async def handle(self, context: ClickContext) -> None: ...
