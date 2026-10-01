"""WebhookService — endpoint CRUD, test sends, delivery log reads.

Owns the WHY: quota, URL safety at registration, pattern validation,
secret lifecycle (generated here, shown once, stored encrypted), and
owner-cache invalidation on every mutation. The flag gate lives at the
route layer (write-side only); dispatch never sees this class.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from bson import ObjectId

from errors import LimitReachedError, NotFoundError, ValidationError
from infrastructure.crypto import decrypt_secret, encrypt_secret
from infrastructure.logging import get_logger
from infrastructure.safe_fetch import FetchHardError, validate_public_https_url
from repositories.webhook_delivery_repository import WebhookDeliveryRepository
from repositories.webhook_endpoint_repository import WebhookEndpointRepository
from repositories.webhook_event_repository import WebhookEventRepository
from schemas.enums.webhook import DeliveryStatus, WebhookFlavor, WebhookStatus
from schemas.models.webhook import (
    WebhookDeliveryDoc,
    WebhookEndpointDoc,
    WebhookScope,
)
from services.events.contract import DomainEvent
from services.features.catalog import UNLIMITED
from services.webhooks.executor import SECRET_ENC_DOMAIN, DeliveryExecutor
from services.webhooks.matcher import OwnerSubscriptionCache
from services.webhooks.registry import (
    EVENT_REGISTRY,
    TEST_EVENT_SPEC,
    TEST_EVENT_TYPE,
    validate_patterns,
)
from services.webhooks.signing import (
    generate_signing_secret,
    new_webhook_id,
    secret_display_prefix,
)

log = get_logger(__name__)


class WebhookService:
    def __init__(
        self,
        endpoint_repo: WebhookEndpointRepository,
        delivery_repo: WebhookDeliveryRepository,
        event_repo: WebhookEventRepository,
        executor: DeliveryExecutor,
        owner_cache: OwnerSubscriptionCache,
        *,
        master_secret: str,
    ) -> None:
        self._endpoints = endpoint_repo
        self._deliveries = delivery_repo
        self._events = event_repo
        self._executor = executor
        self._cache = owner_cache
        self._master_secret = master_secret

    # ── CRUD ─────────────────────────────────────────────────────────────

    async def create_endpoint(
        self,
        user_id: ObjectId,
        *,
        url: str,
        events: list[str],
        description: str | None,
        scope_links: list[str] | None,
        flavor: WebhookFlavor,
        max_endpoints: int,
    ) -> tuple[WebhookEndpointDoc, str]:
        """Returns (doc, raw signing secret). The secret's only appearance.

        ``max_endpoints`` is the owner's plan limit (``-1`` unlimited); every
        endpoint counts, paused and disabled ones included.
        """
        await self._validate_url(url)
        validate_patterns(events)
        if max_endpoints != UNLIMITED:
            current = await self._endpoints.count_by_user(user_id)
            if current >= max_endpoints:
                raise LimitReachedError(
                    "webhook_endpoints_max", max_=max_endpoints, current=current
                )

        secret = generate_signing_secret()
        now = datetime.now(timezone.utc)
        doc = WebhookEndpointDoc(
            user_id=user_id,
            url=url,
            description=description,
            events=events,
            scope=_scope_from_links(scope_links),
            flavor=flavor,
            status=WebhookStatus.ACTIVE,
            signing_secret_enc=encrypt_secret(
                secret, self._master_secret, domain=SECRET_ENC_DOMAIN
            ),
            signing_secret_prefix=secret_display_prefix(secret),
            created_at=now,
        )
        doc.id = await self._endpoints.insert_endpoint(doc)
        await self._cache.invalidate(str(user_id))
        log.info("webhook_endpoint_created", endpoint_id=str(doc.id))
        return doc, secret

    async def get_endpoint(
        self, endpoint_id: ObjectId, user_id: ObjectId
    ) -> WebhookEndpointDoc:
        doc = await self._endpoints.find_owned(endpoint_id, user_id)
        if doc is None:
            raise NotFoundError("Webhook endpoint not found")
        return doc

    async def list_endpoints(self, user_id: ObjectId) -> list[WebhookEndpointDoc]:
        return await self._endpoints.find_by_user(user_id)

    async def update_endpoint(
        self, endpoint_id: ObjectId, user_id: ObjectId, fields: dict[str, Any]
    ) -> WebhookEndpointDoc:
        doc = await self.get_endpoint(endpoint_id, user_id)
        updates: dict[str, Any] = {}
        if "url" in fields and fields["url"] != doc.url:
            await self._validate_url(fields["url"])
            updates["url"] = fields["url"]
        if "events" in fields:
            validate_patterns(fields["events"])
            updates["events"] = fields["events"]
        if "scope_links" in fields:
            updates["scope"] = _scope_from_links(fields["scope_links"]).model_dump()
        if "description" in fields:
            updates["description"] = fields["description"]
        if "flavor" in fields:
            updates["flavor"] = WebhookFlavor(fields["flavor"]).value
        if "status" in fields:
            updates.update(_status_transition(doc, fields["status"]))
        if updates:
            await self._endpoints.update_fields(endpoint_id, updates)
            await self._cache.invalidate(str(user_id))
        return await self.get_endpoint(endpoint_id, user_id)

    async def reveal_secret(self, endpoint_id: ObjectId, user_id: ObjectId) -> str:
        """The stored secret is encrypted, never hashed (the executor must
        read it back to sign), so revealing it to its owner is an ordinary
        read. A secret the master key can no longer open is reported
        instead of raising a bare crypto error."""
        doc = await self.get_endpoint(endpoint_id, user_id)
        try:
            return decrypt_secret(
                doc.signing_secret_enc, self._master_secret, domain=SECRET_ENC_DOMAIN
            )
        except Exception as exc:
            log.error(
                "webhook_secret_reveal_failed",
                endpoint_id=str(endpoint_id),
                error_type=type(exc).__name__,
            )
            raise ValidationError(
                "The stored secret can no longer be read. Delete this "
                "endpoint and create it again."
            ) from exc

    async def delete_endpoint(self, endpoint_id: ObjectId, user_id: ObjectId) -> None:
        if not await self._endpoints.delete_endpoint(endpoint_id, user_id):
            raise NotFoundError("Webhook endpoint not found")
        await self._cache.invalidate(str(user_id))
        log.info("webhook_endpoint_deleted", endpoint_id=str(endpoint_id))

    # ── Test sends ───────────────────────────────────────────────────────

    async def send_test(
        self, endpoint_id: ObjectId, user_id: ObjectId, event_type: str
    ) -> WebhookDeliveryDoc:
        """Send a sample of any catalog event through the REAL pipeline —
        rendered in the endpoint's flavor, signed for real, logged with
        ``is_test`` — synchronously, so the caller gets the outcome."""
        endpoint = await self.get_endpoint(endpoint_id, user_id)
        spec = (
            TEST_EVENT_SPEC
            if event_type == TEST_EVENT_TYPE
            else EVENT_REGISTRY.get(event_type)
        )
        if spec is None:
            raise ValidationError(f"Unknown event type '{event_type}'")

        event = DomainEvent(type=spec.name, owner_id=str(user_id), data=spec.sample())
        event_oid = await self._events.insert_event(event, is_test=True)
        now = datetime.now(timezone.utc)
        delivery_id = await self._deliveries.insert_row(
            {
                "endpoint_id": endpoint.id,
                "user_id": endpoint.user_id,
                "event_oid": event_oid,
                "event_type": event.type,
                "webhook_id": new_webhook_id(),
                "is_test": True,
                "rendered_body": None,
                "dropped_since_last": 0,
                "status": DeliveryStatus.PENDING.value,
                "attempts": [],
                "attempt_count": 0,
                # Never claimable by the background loop — the synchronous
                # attempt below is the only executor this row meets.
                "next_attempt_at": None,
                "claimed_until": None,
                "created_at": now,
                "completed_at": None,
            }
        )
        row = await self._deliveries.find_by_id(delivery_id)
        await self._executor.attempt(row)
        return await self._deliveries.find_by_id(delivery_id)

    # ── Delivery log ─────────────────────────────────────────────────────

    async def list_deliveries(
        self,
        endpoint_id: ObjectId,
        user_id: ObjectId,
        *,
        page: int,
        page_size: int,
        status: DeliveryStatus | None,
    ) -> tuple[list[WebhookDeliveryDoc], int]:
        await self.get_endpoint(endpoint_id, user_id)  # ownership gate
        return await self._deliveries.list_by_endpoint(
            endpoint_id, page=page, page_size=page_size, status=status
        )

    async def retry_delivery(
        self, endpoint_id: ObjectId, delivery_id: ObjectId, user_id: ObjectId
    ) -> WebhookDeliveryDoc:
        """Manual redelivery: same webhook_id, same rendered body, fresh
        attempt — run synchronously like a test send."""
        await self.get_endpoint(endpoint_id, user_id)
        row = await self._deliveries.find_owned(delivery_id, user_id)
        if row is None or row.endpoint_id != endpoint_id:
            raise NotFoundError("Delivery not found")
        if row.status == DeliveryStatus.PENDING:
            raise ValidationError("Delivery is still pending")
        await self._executor.attempt(row)
        return await self._deliveries.find_by_id(delivery_id)

    # ── Internals ────────────────────────────────────────────────────────

    async def _validate_url(self, url: str) -> None:
        if len(url) > 2048:
            raise ValidationError("URL too long (max 2048 characters)")
        try:
            await validate_public_https_url(url)
        except FetchHardError as exc:
            raise ValidationError(f"Endpoint URL rejected: {exc}") from exc


def _scope_from_links(scope_links: list[str] | None) -> WebhookScope:
    if scope_links is None:
        return WebhookScope()
    ids = []
    for raw in scope_links:
        if not ObjectId.is_valid(raw):
            raise ValidationError(f"Invalid link id in scope: '{raw}'")
        ids.append(ObjectId(raw))
    if not ids:
        raise ValidationError("scope.links must be 'all' or a non-empty list of ids")
    return WebhookScope(links=ids)


def _status_transition(doc: WebhookEndpointDoc, target: str) -> dict[str, Any]:
    """Users may pause/resume; DISABLED is system-set and only exits via
    explicit re-activation (which also clears the failure bookkeeping)."""
    status = WebhookStatus(target)
    if status == WebhookStatus.DISABLED:
        raise ValidationError(
            "'disabled' is system-set; use 'paused' to stop deliveries"
        )
    updates: dict[str, Any] = {"status": status.value}
    if status == WebhookStatus.ACTIVE and doc.status == WebhookStatus.DISABLED:
        updates.update(
            {"disabled_reason": None, "consecutive_failures": 0, "dropped_count": 0}
        )
    return updates
