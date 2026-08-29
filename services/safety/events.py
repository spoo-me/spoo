"""Wire contract for the ``events:safety`` stream.

Same envelope as the click and domain streams so FastStream subscribers
decode payloads natively:

    {"v": "1", "type": "safety.analyze", "__data__": "<json>"}

Producers stay plain redis-py; decoding is drop-don't-poison (malformed
payloads log and return None, never raise).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from infrastructure.logging import get_logger

log = get_logger(__name__)

SAFETY_STREAM = "events:safety"
SAFETY_DLQ_STREAM = "events:safety:dlq"

STREAM_FIELD_VERSION = "v"
STREAM_FIELD_TYPE = "type"
STREAM_FIELD_DATA = "__data__"
_WIRE_VERSION = "1"
_EVENT_TYPE = "safety.analyze"


class SafetyAnalyzeEvent(BaseModel):
    """Request to judge one destination. Immutable fact-shaped payload."""

    model_config = ConfigDict(frozen=True)

    url: str
    # An empty host would upsert a "" verdict row and enforce {"dest.host": ""}.
    host: str = Field(min_length=1)
    registrable_domain: str = ""
    # What surfaced the destination: "report" today; "hot" / "pattern" /
    # "sweep" / "edit" later.
    trigger: str = "report"
    # Trigger-specific snapshot carried into the verdict and review embed
    # (report reasons, counts, the reported code).
    context: dict[str, Any] | None = None


def to_stream_fields(event: SafetyAnalyzeEvent) -> dict[str, str]:
    return {
        STREAM_FIELD_VERSION: _WIRE_VERSION,
        STREAM_FIELD_TYPE: _EVENT_TYPE,
        STREAM_FIELD_DATA: event.model_dump_json(),
    }


def safety_event_from_payload(payload: Any) -> SafetyAnalyzeEvent | None:
    """Decode a consumed payload. None + log on malformed, never raise."""
    try:
        if isinstance(payload, SafetyAnalyzeEvent):
            return payload
        if isinstance(payload, (str, bytes)):
            return SafetyAnalyzeEvent.model_validate_json(payload)
        if isinstance(payload, dict):
            data = payload.get(STREAM_FIELD_DATA, payload)
            if isinstance(data, (str, bytes)):
                return SafetyAnalyzeEvent.model_validate_json(data)
            return SafetyAnalyzeEvent.model_validate(data)
    except (PydanticValidationError, ValueError) as exc:
        log.warning(
            "safety_event_decode_failed",
            error_type=type(exc).__name__,
        )
        return None
    log.warning("safety_event_decode_failed", error_type="unsupported_payload")
    return None
