"""Response DTOs for webhook endpoints, deliveries, and the event catalog.

The signing secret appears exactly once, in WebhookEndpointCreatedResponse.
Every other shape exposes ``signing_secret_prefix`` only.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.schemas.dto.base import ResponseBase
from app.schemas.enums.webhook import DeliveryStatus, WebhookFlavor, WebhookStatus
from app.schemas.models.webhook import (
    DeliveryAttempt,
    WebhookDeliveryDoc,
    WebhookEndpointDoc,
)


class WebhookEndpointResponse(ResponseBase):
    id: str
    url: str
    description: str | None = None
    events: list[str]
    scope_links: list[str] | None = Field(
        default=None, description="Null means all links, including future ones."
    )
    flavor: WebhookFlavor
    status: WebhookStatus
    disabled_reason: str | None = None
    signing_secret_prefix: str
    consecutive_failures: int
    total_deliveries: int
    total_successes: int
    last_delivery_at: int | None = None
    last_success_at: int | None = None
    last_failure_reason: str | None = None
    created_at: int

    @classmethod
    def from_doc(cls, doc: WebhookEndpointDoc) -> WebhookEndpointResponse:
        return cls(
            id=str(doc.id),
            url=doc.url,
            description=doc.description,
            events=doc.events,
            scope_links=(
                None
                if doc.scope.links == "all"
                else [str(oid) for oid in doc.scope.links]
            ),
            flavor=doc.flavor,
            status=doc.status,
            disabled_reason=(
                doc.disabled_reason.value if doc.disabled_reason else None
            ),
            signing_secret_prefix=doc.signing_secret_prefix,
            consecutive_failures=doc.consecutive_failures,
            total_deliveries=doc.total_deliveries,
            total_successes=doc.total_successes,
            last_delivery_at=(
                int(doc.last_delivery_at.timestamp()) if doc.last_delivery_at else None
            ),
            last_success_at=(
                int(doc.last_success_at.timestamp()) if doc.last_success_at else None
            ),
            last_failure_reason=doc.last_failure_reason,
            created_at=int(doc.created_at.timestamp()) if doc.created_at else 0,
        )


class WebhookEndpointCreatedResponse(WebhookEndpointResponse):
    signing_secret: str = Field(
        description=(
            "The full signing secret — shown ONCE, never retrievable again. "
            "Store it; you need it to verify webhook signatures."
        )
    )


class WebhookSecretResponse(ResponseBase):
    signing_secret: str = Field(
        description="The full signing secret, revealed to the endpoint owner."
    )


class WebhookEndpointsListResponse(ResponseBase):
    endpoints: list[WebhookEndpointResponse]


class DeliveryAttemptResponse(ResponseBase):
    attempted_at: int
    status_code: int | None = None
    duration_ms: int | None = None
    error: str | None = None
    response_body: str | None = None

    @classmethod
    def from_model(cls, a: DeliveryAttempt) -> DeliveryAttemptResponse:
        return cls(
            attempted_at=int(a.attempted_at.timestamp()),
            status_code=a.status_code,
            duration_ms=a.duration_ms,
            error=a.error,
            response_body=a.response_body,
        )


class WebhookDeliveryResponse(ResponseBase):
    id: str
    webhook_id: str
    event_type: str
    is_test: bool
    status: DeliveryStatus
    attempt_count: int
    attempts: list[DeliveryAttemptResponse]
    next_attempt_at: int | None = None
    rendered_body: str | None = None
    created_at: int

    @classmethod
    def from_doc(cls, doc: WebhookDeliveryDoc) -> WebhookDeliveryResponse:
        return cls(
            id=str(doc.id),
            webhook_id=doc.webhook_id,
            event_type=doc.event_type,
            is_test=doc.is_test,
            status=doc.status,
            attempt_count=doc.attempt_count,
            attempts=[DeliveryAttemptResponse.from_model(a) for a in doc.attempts],
            next_attempt_at=(
                int(doc.next_attempt_at.timestamp()) if doc.next_attempt_at else None
            ),
            rendered_body=doc.rendered_body,
            created_at=int(doc.created_at.timestamp()) if doc.created_at else 0,
        )


class DeliveriesListResponse(ResponseBase):
    deliveries: list[WebhookDeliveryResponse]
    total: int
    page: int
    page_size: int


class EventTypeInfoResponse(ResponseBase):
    type: str
    category: str
    description: str
    frequency: str
    sample: dict[str, Any]


class EventTypesResponse(ResponseBase):
    event_types: list[EventTypeInfoResponse]
