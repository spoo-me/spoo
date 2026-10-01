"""Webhooks consumer-group handlers — framework-free like the click
consumers; the worker's FastStream subscribers delegate here.

Two feeds, one dispatcher:
- ``events:clicks`` (existing stream, new ``webhooks`` group): ClickEvent
  → link.clicked adaptation happens here, at read time — the producer
  side of the click pipeline is untouched.
- ``events:domain`` (new stream): the envelope is already a DomainEvent.

Malformed payloads are dropped (never raised) — a payload that cannot
parse today can never parse, and raising would leave it pending forever
(the claim path's DLQ guard is the backstop for transient poison).
"""

from __future__ import annotations

from typing import Any

from app.infrastructure.geoip import GeoIPService
from app.infrastructure.logging import get_logger
from app.services.click.events import click_event_from_payload
from app.services.events.contract import domain_event_from_payload
from app.services.webhooks.dispatcher import WebhookDispatcher
from app.services.webhooks.payloads import build_link_clicked

log = get_logger(__name__)


class WebhookClickConsumer:
    """events:clicks / group ``webhooks`` — adapt + dispatch."""

    def __init__(
        self,
        dispatcher: WebhookDispatcher,
        geoip: GeoIPService | None,
        system_default_domain: str,
    ) -> None:
        self._dispatcher = dispatcher
        self._geoip = geoip
        self._system_default_domain = system_default_domain

    async def consume(self, payload: Any) -> None:
        click = click_event_from_payload(payload)
        if click is None:
            return  # malformed — logged by the decoder, drop
        event = await build_link_clicked(
            click, self._system_default_domain, geoip=self._geoip
        )
        if event is None:
            return  # unowned link — no possible subscriber
        await self._dispatcher.dispatch(event)


class WebhookDomainConsumer:
    """events:domain / group ``webhooks`` — decode + dispatch."""

    def __init__(self, dispatcher: WebhookDispatcher) -> None:
        self._dispatcher = dispatcher

    async def consume(self, payload: Any) -> None:
        event = domain_event_from_payload(payload)
        if event is None:
            return
        await self._dispatcher.dispatch(event)


class WebhookFanoutClickSink:
    """The no-queue-Redis deployment's click feed: wraps the inline click sink
    so link.clicked webhooks work with no queue Redis and no worker —
    tracking first, then best-effort dispatch. Never raises past the
    inner sink; a webhook must not cost a redirect."""

    def __init__(
        self,
        inner: Any,  # ClickEventSink
        dispatcher: WebhookDispatcher,
        geoip: GeoIPService | None,
        system_default_domain: str,
    ) -> None:
        self._inner = inner
        self._dispatcher = dispatcher
        self._geoip = geoip
        self._system_default_domain = system_default_domain

    async def emit(self, event: Any) -> None:
        await self._inner.emit(event)
        try:
            domain_event = await build_link_clicked(
                event, self._system_default_domain, geoip=self._geoip
            )
            if domain_event is not None:
                await self._dispatcher.dispatch(domain_event)
        except Exception as exc:
            log.error(
                "webhook_click_fanout_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
