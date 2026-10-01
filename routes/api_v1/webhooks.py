"""
POST   /api/v1/webhooks                                  — create endpoint (secret shown once)
GET    /api/v1/webhooks                                  — list endpoints
GET    /api/v1/webhooks/event-types                      — public event catalog
GET    /api/v1/webhooks/{id}                             — endpoint detail
PATCH  /api/v1/webhooks/{id}                             — update (url/events/scope/flavor/status)
DELETE /api/v1/webhooks/{id}                             — delete
POST   /api/v1/webhooks/{id}/test                        — send a sample event, synchronously
GET    /api/v1/webhooks/{id}/deliveries                  — delivery log
POST   /api/v1/webhooks/{id}/deliveries/{delivery_id}/retry — manual redelivery

Gating: the `webhooks` feature flag is enforced on the operations that
INITIATE something — create, test sends, manual retries. Passive
management (list, get, patch, delete, delivery log) stays ungated so a
user who loses the flag keeps managing and pausing what they already
have; stopping their background deliveries is an explicit admin action.
Flag failure is an honest 403, never a 404: the catalog endpoint below
is public documentation-as-API, so there is nothing to hide.
"""

from __future__ import annotations

from typing import Annotated

from bson import ObjectId
from fastapi import APIRouter, Depends, Path, Query, Request

from dependencies import Entitled, FeatureFlagSvc, WebhookSvc
from dependencies.auth import (
    WEBHOOKS_MANAGE_SCOPES,
    WEBHOOKS_READ_SCOPES,
    CurrentUser,
    JwtVerifiedUser,
    require_scopes_verified,
)
from errors import ValidationError
from middleware.openapi import AUTH_RESPONSES, ERROR_RESPONSES
from middleware.rate_limiter import Limits, limiter
from schemas.dto.requests.webhook import (
    CreateWebhookEndpointRequest,
    TestWebhookRequest,
    UpdateWebhookEndpointRequest,
)
from schemas.dto.responses.webhook import (
    DeliveriesListResponse,
    EventTypeInfoResponse,
    EventTypesResponse,
    WebhookDeliveryResponse,
    WebhookEndpointCreatedResponse,
    WebhookEndpointResponse,
    WebhookEndpointsListResponse,
    WebhookSecretResponse,
)
from schemas.enums.webhook import DeliveryStatus
from services.feature_flag_service import WEBHOOKS_FLAG
from services.webhooks.registry import EVENT_REGISTRY

router = APIRouter(tags=["Webhooks"])

ManageUser = Annotated[
    CurrentUser, Depends(require_scopes_verified(WEBHOOKS_MANAGE_SCOPES))
]
ReadUser = Annotated[
    CurrentUser, Depends(require_scopes_verified(WEBHOOKS_READ_SCOPES))
]


def _oid(value: str, what: str) -> ObjectId:
    if not ObjectId.is_valid(value):
        raise ValidationError(f"Invalid {what} id")
    return ObjectId(value)


@router.get(
    "/webhooks/event-types",
    operation_id="listWebhookEventTypes",
    summary="List Webhook Event Types",
)
@limiter.limit(Limits.WEBHOOK_EVENT_TYPES)
async def list_event_types(request: Request) -> EventTypesResponse:
    """The public webhook event catalog — documentation as API.

    Every subscribable event type with its category, firing frequency, and
    an exact sample payload (the same fixture test sends deliver).

    **Authentication**: None.
    """
    return EventTypesResponse(
        event_types=[
            EventTypeInfoResponse(
                type=spec.name,
                category=spec.category,
                description=spec.description,
                frequency=spec.frequency,
                sample=spec.sample(),
            )
            for spec in EVENT_REGISTRY.values()
        ]
    )


@router.post(
    "/webhooks",
    status_code=201,
    responses=AUTH_RESPONSES,
    operation_id="createWebhookEndpoint",
    summary="Create Webhook Endpoint",
)
@limiter.limit(Limits.WEBHOOK_CREATE)
async def create_endpoint(
    request: Request,
    body: CreateWebhookEndpointRequest,
    user: ManageUser,
    webhook_service: WebhookSvc,
    flag_svc: FeatureFlagSvc,
    entitlements: Entitled,
) -> WebhookEndpointCreatedResponse:
    """Register an HTTPS endpoint to receive event deliveries.

    The `signing_secret` is returned **only in this response** — store it;
    it is what your server uses to verify `webhook-signature` headers
    (Standard Webhooks, HMAC-SHA256).

    **Authentication**: Required (verified email). Feature-flagged.

    **Rate Limits**: 10/hour
    """
    await flag_svc.require(WEBHOOKS_FLAG, user, entitlements=entitlements)
    doc, secret = await webhook_service.create_endpoint(
        user.user_id,
        url=body.url,
        events=body.events,
        description=body.description,
        scope_links=body.scope_links,
        flavor=body.flavor,
        max_endpoints=entitlements.limit("webhook_endpoints_max"),
    )
    base = WebhookEndpointResponse.from_doc(doc)
    return WebhookEndpointCreatedResponse(**base.model_dump(), signing_secret=secret)


@router.get(
    "/webhooks",
    responses=AUTH_RESPONSES,
    operation_id="listWebhookEndpoints",
    summary="List Webhook Endpoints",
)
@limiter.limit(Limits.WEBHOOK_READ)
async def list_endpoints(
    request: Request,
    user: ReadUser,
    webhook_service: WebhookSvc,
) -> WebhookEndpointsListResponse:
    """List all webhook endpoints on the account (secrets never included)."""
    docs = await webhook_service.list_endpoints(user.user_id)
    return WebhookEndpointsListResponse(
        endpoints=[WebhookEndpointResponse.from_doc(d) for d in docs]
    )


@router.get(
    "/webhooks/{endpoint_id}",
    responses={**AUTH_RESPONSES, **ERROR_RESPONSES},
    operation_id="getWebhookEndpoint",
    summary="Get Webhook Endpoint",
)
@limiter.limit(Limits.WEBHOOK_READ)
async def get_endpoint(
    request: Request,
    endpoint_id: Annotated[str, Path()],
    user: ReadUser,
    webhook_service: WebhookSvc,
) -> WebhookEndpointResponse:
    doc = await webhook_service.get_endpoint(
        _oid(endpoint_id, "endpoint"), user.user_id
    )
    return WebhookEndpointResponse.from_doc(doc)


@router.get(
    "/webhooks/{endpoint_id}/secret",
    responses={**AUTH_RESPONSES, **ERROR_RESPONSES},
    operation_id="revealWebhookSecret",
    summary="Reveal Signing Secret",
)
@limiter.limit(Limits.WEBHOOK_READ)
async def reveal_secret(
    request: Request,
    endpoint_id: Annotated[str, Path()],
    user: JwtVerifiedUser,
    webhook_service: WebhookSvc,
) -> WebhookSecretResponse:
    """The full signing secret, for the endpoint owner. Secrets are stored
    encrypted and readable on demand; treat the response like a password.

    **Authentication**: Interactive session only — API keys are refused,
    like every operation that exposes or mints credential material."""
    secret = await webhook_service.reveal_secret(
        _oid(endpoint_id, "endpoint"), user.user_id
    )
    return WebhookSecretResponse(signing_secret=secret)


@router.patch(
    "/webhooks/{endpoint_id}",
    responses={**AUTH_RESPONSES, **ERROR_RESPONSES},
    operation_id="updateWebhookEndpoint",
    summary="Update Webhook Endpoint",
)
@limiter.limit(Limits.WEBHOOK_WRITE)
async def update_endpoint(
    request: Request,
    endpoint_id: Annotated[str, Path()],
    body: UpdateWebhookEndpointRequest,
    user: ManageUser,
    webhook_service: WebhookSvc,
) -> WebhookEndpointResponse:
    """Update url, events, scope, flavor, description, or pause/resume.

    A new `url` is re-checked (https + public address) before it takes
    effect. `status` accepts `active`/`paused`; `disabled` is system-set —
    re-activating a disabled endpoint clears its failure bookkeeping.
    """
    fields = body.model_dump(exclude_unset=True)
    doc = await webhook_service.update_endpoint(
        _oid(endpoint_id, "endpoint"), user.user_id, fields
    )
    return WebhookEndpointResponse.from_doc(doc)


@router.delete(
    "/webhooks/{endpoint_id}",
    status_code=204,
    responses={**AUTH_RESPONSES, **ERROR_RESPONSES},
    operation_id="deleteWebhookEndpoint",
    summary="Delete Webhook Endpoint",
)
@limiter.limit(Limits.WEBHOOK_WRITE)
async def delete_endpoint(
    request: Request,
    endpoint_id: Annotated[str, Path()],
    user: ManageUser,
    webhook_service: WebhookSvc,
) -> None:
    await webhook_service.delete_endpoint(_oid(endpoint_id, "endpoint"), user.user_id)


@router.post(
    "/webhooks/{endpoint_id}/test",
    responses={**AUTH_RESPONSES, **ERROR_RESPONSES},
    operation_id="testWebhookEndpoint",
    summary="Send Test Event",
)
@limiter.limit(Limits.WEBHOOK_TEST)
async def test_endpoint(
    request: Request,
    endpoint_id: Annotated[str, Path()],
    body: TestWebhookRequest,
    user: ManageUser,
    webhook_service: WebhookSvc,
    flag_svc: FeatureFlagSvc,
) -> WebhookDeliveryResponse:
    """Send a sample of any catalog event through the real pipeline —
    rendered in the endpoint's flavor, signed with its real secret — and
    return the delivery outcome synchronously."""
    # Test sends initiate outbound calls, so they carry the same flag gate
    # as creation; passive management of existing endpoints does not.
    await flag_svc.require(WEBHOOKS_FLAG, user)
    row = await webhook_service.send_test(
        _oid(endpoint_id, "endpoint"), user.user_id, body.event_type
    )
    return WebhookDeliveryResponse.from_doc(row)


@router.get(
    "/webhooks/{endpoint_id}/deliveries",
    responses={**AUTH_RESPONSES, **ERROR_RESPONSES},
    operation_id="listWebhookDeliveries",
    summary="List Webhook Deliveries",
)
@limiter.limit(Limits.WEBHOOK_READ)
async def list_deliveries(
    request: Request,
    endpoint_id: Annotated[str, Path()],
    user: ReadUser,
    webhook_service: WebhookSvc,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
    status: Annotated[DeliveryStatus | None, Query()] = None,
) -> DeliveriesListResponse:
    """The delivery log: what was sent, when, each attempt's outcome, and
    the exact rendered body (30-day retention)."""
    rows, total = await webhook_service.list_deliveries(
        _oid(endpoint_id, "endpoint"),
        user.user_id,
        page=page,
        page_size=page_size,
        status=status,
    )
    return DeliveriesListResponse(
        deliveries=[WebhookDeliveryResponse.from_doc(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post(
    "/webhooks/{endpoint_id}/deliveries/{delivery_id}/retry",
    responses={**AUTH_RESPONSES, **ERROR_RESPONSES},
    operation_id="retryWebhookDelivery",
    summary="Retry Delivery",
)
@limiter.limit(Limits.WEBHOOK_RETRY)
async def retry_delivery(
    request: Request,
    endpoint_id: Annotated[str, Path()],
    delivery_id: Annotated[str, Path()],
    user: ManageUser,
    webhook_service: WebhookSvc,
    flag_svc: FeatureFlagSvc,
) -> WebhookDeliveryResponse:
    """Redeliver a completed delivery: same `webhook-id`, same body, fresh
    attempt — consumers dedup on `webhook-id`."""
    await flag_svc.require(WEBHOOKS_FLAG, user)
    row = await webhook_service.retry_delivery(
        _oid(endpoint_id, "endpoint"),
        _oid(delivery_id, "delivery"),
        user.user_id,
    )
    return WebhookDeliveryResponse.from_doc(row)
