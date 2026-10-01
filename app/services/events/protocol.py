"""DomainEventSink protocol — producers depend on this, never on transports.

Emit is awaited-but-never-raises from the producer's perspective: an event
side effect must never fail the primary action (same posture as click
emission and ops_notify sends). Implementations own that guarantee.
"""

from __future__ import annotations

from typing import Protocol

from app.services.events.contract import DomainEvent


class DomainEventSink(Protocol):
    async def emit(self, event: DomainEvent) -> None: ...
