"""ClickEventSink — the boundary between the redirect route and click processing."""

from __future__ import annotations

from typing_extensions import Protocol

from app.services.click.events import ClickEvent


class ClickEventSink(Protocol):
    """Accepts a click event for processing (inline or out-of-band).

    Raises (inline backend only — the stream backend never raises for
    processing failures, it degrades to the inline fallback instead):
        ValidationError: Invalid or missing User-Agent.
        ForbiddenError:  Bot blocked for v1/emoji URLs (redirect blocked).
    """

    async def emit(self, event: ClickEvent) -> None: ...
