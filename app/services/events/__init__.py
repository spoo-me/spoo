"""Domain-event backbone — envelope contract, sink protocol, transports.

NOT webhook-specific: the webhooks dispatcher is one consumer; future
consumers (alerts, event log, audit) register their own consumer groups
on the same streams without touching this package.
"""

from app.services.events.contract import DOMAIN_EVENTS_STREAM, DomainEvent
from app.services.events.protocol import DomainEventSink
from app.services.events.sinks import (
    InlineDomainEventSink,
    NullDomainEventSink,
    StreamDomainEventSink,
)

__all__ = [
    "DOMAIN_EVENTS_STREAM",
    "DomainEvent",
    "DomainEventSink",
    "InlineDomainEventSink",
    "NullDomainEventSink",
    "StreamDomainEventSink",
]
