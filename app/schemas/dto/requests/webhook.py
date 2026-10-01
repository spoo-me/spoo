"""Request DTOs for webhook endpoint management."""

from __future__ import annotations

from pydantic import Field, field_validator

from app.schemas.dto.base import RequestBase
from app.schemas.enums.webhook import WebhookFlavor, WebhookStatus


class _WebhookFieldsMixin(RequestBase):
    @field_validator("events", check_fields=False)
    @classmethod
    def _events_not_empty(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and not v:
            raise ValueError("events must contain at least one event type or pattern")
        return v


class CreateWebhookEndpointRequest(_WebhookFieldsMixin):
    url: str = Field(
        description="HTTPS URL that receives event deliveries.",
        examples=["https://example.com/hooks/spoo"],
        max_length=2048,
    )
    events: list[str] = Field(
        description=(
            "Event types or patterns to subscribe to. Supports `link.*`-style "
            "category wildcards and `*` for everything."
        ),
        examples=[["link.clicked", "link.expired"]],
        min_length=1,
        max_length=32,
    )
    description: str | None = Field(default=None, max_length=256)
    scope_links: list[str] | None = Field(
        default=None,
        description=(
            "Link IDs to scope deliveries to. Omit (or null) for all links, "
            "including ones created later."
        ),
        max_length=256,
    )
    flavor: WebhookFlavor = Field(
        default=WebhookFlavor.RAW,
        description="Payload presentation. `raw` is the documented contract.",
    )


class UpdateWebhookEndpointRequest(_WebhookFieldsMixin):
    """All fields optional; only provided fields change. `scope_links: null`
    is 'all links' — to keep an existing scope, omit the field."""

    url: str | None = Field(default=None, max_length=2048)
    events: list[str] | None = Field(default=None, min_length=1, max_length=32)
    description: str | None = Field(default=None, max_length=256)
    scope_links: list[str] | None = Field(default=None, max_length=256)
    flavor: WebhookFlavor | None = None
    status: WebhookStatus | None = Field(
        default=None,
        description="`active` or `paused`. `disabled` is system-set.",
    )


class TestWebhookRequest(RequestBase):
    event_type: str = Field(
        default="webhook.test",
        description=(
            "Any catalog event type — its documented sample payload is sent "
            "through the real pipeline — or `webhook.test`."
        ),
        examples=["link.clicked"],
    )
