"""Stats consumer — persists click analytics from the event stream.

Runs in the click worker as consumer group ``stats``. Replays events
through the untouched :class:`ClickService` — same UA parsing, GeoIP
lookups, and Mongo writes as inline mode, just out-of-band.
"""

from __future__ import annotations

from typing import Any

from app.errors import ForbiddenError, ValidationError
from app.infrastructure.logging import get_logger
from app.services.click.events import click_event_from_payload
from app.services.click.service import ClickService

log = get_logger(__name__)


class StatsClickConsumer:
    """Failure semantics (the at-least-once contract):

    - undecodable payload      -> drop: can never succeed, don't poison
    - ValidationError (bad UA) -> drop: same terminal outcome as inline mode
    - ForbiddenError (v1 bot)  -> drop: the redirect was already served and
      inline mode records nothing for blocked bots either
    - anything else            -> raise: the message stays pending and is
      retried via the claimer (Mongo/GeoIP transient failures)
    """

    def __init__(self, click_service: ClickService) -> None:
        self._click_service = click_service

    async def consume(self, payload: Any) -> None:
        event = click_event_from_payload(payload)
        if event is None:
            return
        try:
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
                clicked_at=event.enqueued_at,
            )
        except ValidationError:
            log.info(
                "click_event_skipped_validation",
                short_code=event.short_code,
                schema=event.schema_key,
            )
        except ForbiddenError:
            log.info(
                "click_event_bot_dropped",
                short_code=event.short_code,
                schema=event.schema_key,
            )
