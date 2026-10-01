"""ClickEvent — the serializable fact emitted for every tracked redirect.

The event snapshots everything the click pipeline needs (including the
resolved ``UrlCacheData``) so consumers never re-resolve the URL — a
re-resolve would reintroduce the Mongo round-trip the async pipeline
exists to remove.

Wire format
-----------
Events travel as flat fields on a Redis Stream entry:

    {"v": "1", "type": "click.recorded", "__data__": "<event json>"}

``__data__`` is the payload key FastStream's parser extracts natively, so
subscriber handlers receive the decoded event dict without custom parsers,
while the producer side stays plain redis-py (``XADD``) with no framework
dependency on the hot path. ``v``/``type`` ride alongside for redis-cli
introspection and future multi-event streams.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic import ValidationError as PydanticValidationError

from app.infrastructure.cache.url_cache import UrlCacheData
from app.infrastructure.logging import get_logger

log = get_logger(__name__)

STREAM_FIELD_VERSION = "v"
STREAM_FIELD_TYPE = "type"
STREAM_FIELD_DATA = "__data__"
EVENT_TYPE_CLICK = "click.recorded"
_WIRE_VERSION = "1"

# UTM values are visitor-controlled query-string input — bound their size so
# a crafted link can't bloat stream payloads, and strip control characters
# before they reach Mongo/logs.
_UTM_MAX_LEN = 100
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class ClickEvent(BaseModel):
    """Immutable fact: a redirect was served and its click should be tracked."""

    model_config = ConfigDict(frozen=True)

    short_code: str
    # Resolved schema key ("v1" | "v2" | "emoji") — distinct from
    # url.schema_version (emoji aliases resolve with schema_version "v1").
    # Named schema_key because `schema` shadows a BaseModel attribute.
    schema_key: str
    is_emoji: bool
    url: UrlCacheData
    client_ip: str
    user_agent: str
    referrer: str | None
    cf_city: str | None
    # Geo-targeting decision audit: the ISO alpha-2 code the redirect router
    # used (None when the link has no geo rules) and whether a rule matched.
    # Defaults keep pre-existing stream payloads decodable.
    resolved_country: str | None = None
    geo_matched: bool = False
    # Index into url.ab_variants the redirect served; None = default URL.
    variant_index: int | None = None
    # Campaign tags from the short link's query string. Defaults keep
    # pre-existing stream payloads decodable.
    utm_source: str | None = None
    utm_medium: str | None = None
    utm_campaign: str | None = None
    redirect_ms: int
    enqueued_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("utm_source", "utm_medium", "utm_campaign")
    @classmethod
    def _sanitize_utm(cls, value: str | None) -> str | None:
        """Enforced structurally (like the password-hash strip below) so
        every producer inherits the bound instead of remembering it."""
        if value is None:
            return None
        value = _CONTROL_CHARS_RE.sub("", value).strip()[:_UTM_MAX_LEN]
        return value or None

    @field_validator("url")
    @classmethod
    def _strip_password_hash(cls, url: UrlCacheData) -> UrlCacheData:
        """v1 "hashes" are PLAINTEXT passwords — they must never ride the
        stream (queue Redis, DLQ, logs). Enforced structurally here so every
        producer (the redirect route today, the edge beacon tomorrow)
        inherits the guarantee instead of having to remember it."""
        if url.password_hash is not None:
            return url.model_copy(update={"password_hash": None})
        return url


def to_stream_fields(event: ClickEvent) -> dict[str, str]:
    """Encode an event as flat string fields for ``XADD``."""
    return {
        STREAM_FIELD_VERSION: _WIRE_VERSION,
        STREAM_FIELD_TYPE: EVENT_TYPE_CLICK,
        STREAM_FIELD_DATA: event.model_dump_json(),
    }


def _error_summary(exc: PydanticValidationError) -> list[dict[str, Any]]:
    """Field locations + error types, with input values stripped (PII)."""
    return [
        {"loc": e["loc"], "type": e["type"]}
        for e in exc.errors(include_url=False, include_input=False)
    ]


def from_stream_fields(fields: dict[str, str]) -> ClickEvent | None:
    """Decode raw stream entry fields back into a ClickEvent.

    Returns None (and logs) on malformed payloads — a payload that cannot
    parse today can never parse, so callers drop it instead of letting it
    poison-pill a consumer group.
    """
    raw = fields.get(STREAM_FIELD_DATA)
    if not raw:
        log.warning("click_event_missing_data", fields=list(fields.keys()))
        return None
    try:
        return ClickEvent.model_validate_json(raw)
    except PydanticValidationError as exc:
        # Structural diagnostics only — the raw payload carries PII
        # (client_ip, user_agent, long_url) that must not reach logs.
        log.warning("click_event_malformed", errors=_error_summary(exc))
        return None


def click_event_from_payload(payload: Any) -> ClickEvent | None:
    """Decode an already-JSON-parsed payload (the FastStream handler path).

    FastStream extracts and JSON-decodes the ``__data__`` field before the
    handler runs, so subscribers receive a dict rather than raw fields.
    Same drop-don't-poison semantics as :func:`from_stream_fields`.
    """
    if not isinstance(payload, dict):
        log.warning("click_event_payload_not_dict", payload_type=type(payload).__name__)
        return None
    try:
        return ClickEvent.model_validate(payload)
    except PydanticValidationError as exc:
        log.warning("click_event_malformed", errors=_error_summary(exc))
        return None
