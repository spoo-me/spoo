"""DomainEvent — the pinned wire contract for the ``events:domain`` stream.

Low-volume CRUD-shaped facts (link lifecycle) ride this stream; high-volume click facts stay on
``events:clicks`` with their own contract (services/click/events.py).
Consumers in any process decode this envelope — treat changes with the
same discipline as edge_cache/contract.py: additive evolution only,
remove/rename requires a version bump.

Wire format mirrors the click stream so FastStream subscribers decode
payloads natively:

    {"v": "1", "type": "<event type>", "__data__": "<envelope json>"}
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from app.infrastructure.logging import get_logger

log = get_logger(__name__)

DOMAIN_EVENTS_STREAM = "events:domain"

STREAM_FIELD_VERSION = "v"
STREAM_FIELD_TYPE = "type"
STREAM_FIELD_DATA = "__data__"
_WIRE_VERSION = "1"


def new_event_id() -> str:
    return f"evt_{uuid.uuid4().hex}"


class DomainEvent(BaseModel):
    """Immutable fact: something happened to a resource an owner cares about.

    ``data`` is validated against the event type's registry payload model
    at publish time (services/webhooks/registry.py) — the envelope itself
    stays schema-agnostic so new event types never touch this contract.
    """

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(default_factory=new_event_id)
    type: str
    v: int = 1
    owner_id: str
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    data: dict[str, Any]


def to_stream_fields(event: DomainEvent) -> dict[str, str]:
    """Encode an event as flat string fields for ``XADD``."""
    return {
        STREAM_FIELD_VERSION: _WIRE_VERSION,
        STREAM_FIELD_TYPE: event.type,
        STREAM_FIELD_DATA: event.model_dump_json(),
    }


def _error_summary(exc: PydanticValidationError) -> list[dict[str, Any]]:
    """Field locations + error types, with input values stripped (PII)."""
    return [
        {"loc": e["loc"], "type": e["type"]}
        for e in exc.errors(include_url=False, include_input=False)
    ]


def domain_event_from_payload(payload: Any) -> DomainEvent | None:
    """Decode an already-JSON-parsed payload (the FastStream handler path).

    Returns None (and logs) on malformed payloads — a payload that cannot
    parse today can never parse, so callers drop it instead of letting it
    poison-pill a consumer group.
    """
    if not isinstance(payload, dict):
        log.warning(
            "domain_event_payload_not_dict", payload_type=type(payload).__name__
        )
        return None
    try:
        return DomainEvent.model_validate(payload)
    except PydanticValidationError as exc:
        log.warning("domain_event_malformed", errors=_error_summary(exc))
        return None
