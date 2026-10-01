"""MetaImageValidateEvent — async validation request for an external og:image.

Wire format mirrors services/click/events.py exactly: flat stream fields
with the ``__data__`` payload key FastStream extracts natively (and which
workers/dlq.py's guard expects).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from app.infrastructure.logging import get_logger

log = get_logger(__name__)

STREAM_FIELD_VERSION = "v"
STREAM_FIELD_TYPE = "type"
STREAM_FIELD_DATA = "__data__"
EVENT_TYPE_META_IMAGE = "meta.image.validate"
_WIRE_VERSION = "1"


class MetaImageValidateEvent(BaseModel):
    """Validate one external https og:image out-of-band.

    ``alias``/``domain`` ride along so the consumer can invalidate the URL
    cache (and re-sync the edge KV entry) without a re-read; ``image_url``
    doubles as the CAS token — a user edit that changes the image makes
    this event's writes no-ops.
    """

    model_config = ConfigDict(frozen=True)

    url_id: str  # str(ObjectId)
    alias: str
    domain: str
    image_url: str
    requested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def to_stream_fields(event: MetaImageValidateEvent) -> dict[str, str]:
    return {
        STREAM_FIELD_VERSION: _WIRE_VERSION,
        STREAM_FIELD_TYPE: EVENT_TYPE_META_IMAGE,
        STREAM_FIELD_DATA: event.model_dump_json(),
    }


def meta_image_event_from_payload(payload: Any) -> MetaImageValidateEvent | None:
    """Drop-don't-poison decode (FastStream hands us the parsed __data__)."""
    if not isinstance(payload, dict):
        log.warning(
            "meta_image_event_payload_not_dict", payload_type=type(payload).__name__
        )
        return None
    try:
        return MetaImageValidateEvent.model_validate(payload)
    except PydanticValidationError as exc:
        log.warning(
            "meta_image_event_malformed",
            errors=[
                {"loc": e["loc"], "type": e["type"]}
                for e in exc.errors(include_url=False, include_input=False)
            ],
        )
        return None
