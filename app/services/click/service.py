"""
ClickService — thin dispatcher that delegates to injected handlers.

Adding support for a new URL schema never requires touching this file:
create a class implementing ClickHandler and register it in the handlers
dict at the composition root.

The ``"v1"`` handler is used as fallback for any unknown schema (covers
``"emoji"`` and legacy codes).
"""

from __future__ import annotations

from datetime import datetime

from app.infrastructure.cache.url_cache import UrlCacheData
from app.services.click.protocol import ClickContext, ClickHandler


class ClickService:
    """Dispatches click tracking to the appropriate schema handler.

    Args:
        handlers: Mapping from schema key → ClickHandler.
                  Must include at least a ``"v1"`` key used as fallback.
    """

    def __init__(self, handlers: dict[str, ClickHandler]) -> None:
        self._handlers = handlers

    async def track_click(
        self,
        url_data: UrlCacheData,
        short_code: str,
        schema: str,
        is_emoji: bool,
        client_ip: str,
        redirect_ms: int,
        user_agent: str,
        referrer: str | None,
        clicked_at: datetime,
        cf_city: str | None = None,
        utm_source: str | None = None,
        utm_medium: str | None = None,
        utm_campaign: str | None = None,
        variant_index: int | None = None,
    ) -> None:
        """
        Dispatch click tracking to the appropriate handler.

        Falls back to the ``"v1"`` handler when ``schema`` is not in the
        handlers dict (covers ``"emoji"`` and any unknown schema).

        Raises:
            ValidationError: Invalid or missing User-Agent (both v1 and v2).
            ForbiddenError:  Bot blocked for v1/emoji URLs (redirect blocked).
        """
        handler = self._handlers.get(schema, self._handlers["v1"])
        context = ClickContext(
            url_data=url_data,
            short_code=short_code,
            client_ip=client_ip,
            redirect_ms=redirect_ms,
            user_agent=user_agent,
            referrer=referrer,
            clicked_at=clicked_at,
            is_emoji=is_emoji,
            cf_city=cf_city,
            utm_source=utm_source,
            utm_medium=utm_medium,
            utm_campaign=utm_campaign,
            variant_index=variant_index,
        )
        await handler.handle(context)
