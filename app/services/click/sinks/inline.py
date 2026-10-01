"""InlineSink — synchronous click processing on the request path."""

from __future__ import annotations

from app.services.click.events import ClickEvent
from app.services.click.service import ClickService


class InlineSink:
    """Default sink: byte-for-byte the pre-sink behavior.

    Same GeoIP lookups, same Mongo writes, same exceptions surfacing to
    the route. Deployments that configure nothing get exactly this.
    """

    def __init__(self, click_service: ClickService) -> None:
        self._click_service = click_service

    async def emit(self, event: ClickEvent) -> None:
        await self._click_service.track_click(
            url_data=event.url,
            short_code=event.short_code,
            schema=event.schema_key,
            is_emoji=event.is_emoji,
            client_ip=event.client_ip,
            redirect_ms=event.redirect_ms,
            user_agent=event.user_agent,
            referrer=event.referrer,
            cf_city=event.cf_city,
            utm_source=event.utm_source,
            utm_medium=event.utm_medium,
            utm_campaign=event.utm_campaign,
            variant_index=event.variant_index,
        )
