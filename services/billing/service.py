"""
Billing: checkout links, the customer portal, and turning provider webhooks
into subscription transitions.

Webhook handling, in order: verify the signature (401, nothing stored);
record the event id (an applied or ignored duplicate stops here with 200);
take a short lock on the subscription or transaction (a concurrent duplicate
gets 409); re-fetch the entity from the provider and act on that, never on
the payload; write the transition through the entitlement service. Any
exception marks the row failed and bubbles as a 500 so the provider retries,
and a failed row is retried, not deduped.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from bson import ObjectId

from config import BillingSettings
from errors import (
    BillingProviderError,
    ConflictError,
    InvalidTransitionError,
    NotFoundError,
)
from infrastructure.logging import get_logger
from repositories.billing_event_repository import BillingEventRepository
from repositories.subscription_repository import SubscriptionRepository
from schemas.models.billing_event import BillingEventDoc, BillingEventOutcome
from schemas.models.subscription import (
    SubscriptionDoc,
    SubscriptionKind,
    SubscriptionProvider,
    SubscriptionStatus,
)
from services.billing.port import BillingProvider, WebhookEvent
from services.entitlements.service import EntitlementService
from services.entitlements.state_machine import SubscriptionEvent, is_noop
from services.entitlements.terms import GRACE_DAYS, PREPAID_DAYS
from services.features.catalog import Plan
from shared.datetime_utils import as_aware_utc

log = get_logger(__name__)

_LOCK_SECONDS = 30
# Events that carry no dates of their own: a repeat in the same status is
# nothing to write, unlike a renewal that moves the period end.
_STATE_ONLY_EVENTS = {
    SubscriptionEvent.TERM_ENDED,
    SubscriptionEvent.PAST_DUE_CEILING,
    SubscriptionEvent.CANCEL_SCHEDULED,
    SubscriptionEvent.CANCEL_REVOKED,
    SubscriptionEvent.PAYMENT_FAILED,
}
_FOUNDING_RENEWAL_STATUSES = {SubscriptionStatus.ACTIVE, SubscriptionStatus.GRACE}
# The provider still bills these; a second monthly checkout would bill twice.
_LIVE_RECURRING = {
    SubscriptionStatus.ACTIVE,
    SubscriptionStatus.PAST_DUE,
    SubscriptionStatus.CANCEL_AT_PERIOD_END,
}
_NOT_YET_APPLIED = {BillingEventOutcome.RECEIVED, BillingEventOutcome.FAILED}


class BillingService:
    def __init__(
        self,
        provider: BillingProvider,
        entitlements: EntitlementService,
        subscriptions: SubscriptionRepository,
        events: BillingEventRepository,
        redis,
        settings: BillingSettings,
        *,
        app_url: str,
    ) -> None:
        self._provider = provider
        self._entitlements = entitlements
        self._subs = subscriptions
        self._events = events
        self._redis = redis
        self._settings = settings
        self._app_url = app_url.rstrip("/")

    # ── Checkout and portal ──────────────────────────────────────────────

    async def founding_status(self) -> tuple[int, datetime | None]:
        """(seats left, window end). Seats count founding subscriptions here;
        the provider's discount usage limit is the second fence."""
        until = self._settings.founding_until
        if until is None:
            return 0, None
        used = await self._subs.count_founding()
        return max(0, self._settings.founding_seats - used), until

    async def _founding_discount(
        self, existing: SubscriptionDoc | None, cadence: str
    ) -> str | None:
        # One Paddle discount holds one flat amount, so each cadence has its own.
        settings = self._settings
        if existing is not None and existing.founding:
            if (
                existing.founding_streak_ok
                and existing.status in _FOUNDING_RENEWAL_STATUSES
            ):
                return _by_cadence(
                    settings.paddle_discount_founding_renew_monthly,
                    settings.paddle_discount_founding_renew_year,
                    cadence,
                )
            return None
        seats_left, until = await self.founding_status()
        if until is None or datetime.now(timezone.utc) >= until or seats_left <= 0:
            return None
        return _by_cadence(
            settings.paddle_discount_founding_first_monthly,
            settings.paddle_discount_founding_first_year,
            cadence,
        )

    async def checkout(
        self, user_id: ObjectId, cadence: str, *, from_: str | None, return_to: str
    ) -> str:
        price_id = (
            self._settings.paddle_price_pro_year
            if cadence == "year"
            else self._settings.paddle_price_pro_monthly
        )
        existing = await self._subs.find_by_user(user_id)
        if cadence == "monthly" and existing is not None and _holds_a_term(existing):
            raise ConflictError("already_subscribed")
        discount_id = await self._founding_discount(existing, cadence)
        custom_data: dict[str, Any] = {
            "user_id": str(user_id),
            "cadence": cadence,
            "return_to": return_to,
            "founding": discount_id is not None,
        }
        if from_:
            custom_data["from"] = from_
        page = f"{self._app_url}/upgrade/checkout?return={quote(return_to, safe='/')}"
        if from_:
            page += f"&from={quote(from_)}"
        checkout = await self._provider.create_checkout(
            price_id=price_id,
            discount_id=discount_id,
            custom_data=custom_data,
            checkout_url=page,
        )
        log.info(
            "billing_checkout_created",
            user_id=str(user_id),
            cadence=cadence,
            founding=discount_id is not None,
            transaction_id=checkout.transaction_id,
        )
        return checkout.url

    async def portal(self, user_id: ObjectId) -> str:
        sub = await self._subs.find_by_user(user_id)
        customer_id = sub.provider_ids.get("customer_id") if sub else None
        if not customer_id:
            raise NotFoundError("no billing account for this user")
        sub_id = sub.provider_ids.get("subscription_id")
        return await self._provider.portal_url(customer_id, [sub_id] if sub_id else [])

    # ── Webhooks ─────────────────────────────────────────────────────────

    async def handle_webhook(self, raw: bytes, signature: str | None) -> str:
        event = self._provider.verify_webhook(raw, signature)
        data = event.data
        recorded = await self._events.insert_new(
            BillingEventDoc(
                event_id=event.event_id,
                type=event.event_type,
                occurred_at=event.occurred_at,
                subscription_id=_subscription_ref(event),
                transaction_id=_transaction_ref(event),
                payload={"event_type": event.event_type, "data": data},
                received_at=datetime.now(timezone.utc),
            )
        )
        if not recorded and await self._already_applied(event.event_id):
            return "duplicate"

        lock_key = f"billing:lock:{_subscription_ref(event) or _transaction_ref(event) or event.event_id}"
        token = await self._lock(lock_key)
        if token is None:
            raise ConflictError("another delivery for this subscription is in flight")
        try:
            # A twin that read the row before we applied it waits on the lock.
            if not recorded and await self._already_applied(event.event_id):
                return "duplicate"
            try:
                outcome, detail = await self._apply(event)
            except Exception as exc:
                await self._events.set_outcome(
                    event.event_id, BillingEventOutcome.FAILED, str(exc)[:200]
                )
                raise
            await self._events.set_outcome(event.event_id, outcome, detail)
        finally:
            await self._unlock(lock_key, token)
        log.info(
            "billing_webhook_handled",
            event_id=event.event_id,
            event_type=event.event_type,
            outcome=outcome.value,
            detail=detail,
        )
        return outcome.value

    async def _already_applied(self, event_id: str) -> bool:
        """RECEIVED or FAILED means not yet applied; redelivery retries it."""
        earlier = await self._events.find_by_event_id(event_id)
        if earlier is None or earlier.outcome in _NOT_YET_APPLIED:
            return False
        log.info("billing_webhook_duplicate", event_id=event_id)
        return True

    async def _lock(self, key: str) -> str | None:
        if self._redis is None:
            return ""
        token = secrets.token_hex(8)
        ok = await self._redis.set(key, token, nx=True, ex=_LOCK_SECONDS)
        return token if ok else None

    async def _unlock(self, key: str, token: str) -> None:
        # Only the holder deletes: a handler that outlived the TTL must not
        # free a lock a newer delivery now owns.
        if self._redis is not None and await self._redis.get(key) == token:
            await self._redis.delete(key)

    async def _apply(
        self, event: WebhookEvent
    ) -> tuple[BillingEventOutcome, str | None]:
        entity, _, _ = event.event_type.partition(".")
        if entity == "subscription":
            return await self._apply_subscription(event)
        if event.event_type == "transaction.completed":
            return await self._apply_transaction(event)
        if event.event_type == "adjustment.updated":
            return await self._apply_adjustment(event)
        return BillingEventOutcome.IGNORED_NOOP, "unhandled event type"

    async def _apply_subscription(
        self, event: WebhookEvent
    ) -> tuple[BillingEventOutcome, str | None]:
        ps = await self._provider.fetch_subscription(event.data["id"])
        user_id = _user_id(ps.custom_data)
        ours = None
        if user_id is None:
            ours = await self._subs.find_by_provider_subscription(ps.id)
            user_id = ours.user_id if ours else None
        if user_id is None:
            log.error(
                "billing_unknown_user", subscription_id=ps.id, event_id=event.event_id
            )
            return BillingEventOutcome.IGNORED_UNKNOWN, "no user for subscription"
        ours = ours or await self._subs.find_by_user(user_id)
        tracked = ours.provider_ids.get("subscription_id") if ours else None
        if ours is not None and ours.kind is SubscriptionKind.PREPAID:
            # A live prepaid term outranks the subscription it replaced and
            # anything but a fresh active purchase; a lapsed one governs nothing.
            if ours.effective_plan is Plan.PRO and (
                ps.id == tracked or ps.status != "active"
            ):
                return BillingEventOutcome.IGNORED_NOOP, "prepaid term governs"
        elif tracked and ps.id != tracked and ps.status != "active":
            return BillingEventOutcome.IGNORED_STALE, "superseded subscription"

        now = datetime.now(timezone.utc)
        fields: dict[str, Any] = {
            "provider": SubscriptionProvider.PADDLE,
            "kind": SubscriptionKind.RECURRING,
            "provider_ids": {
                **(ours.provider_ids if ours else {}),
                "subscription_id": ps.id,
                "customer_id": ps.customer_id,
            },
        }
        if ps.current_period_end is not None:
            fields["current_period_end"] = ps.current_period_end
        founding_now = self._founding_discount_applied(ps.discount_id)
        founding = founding_now or bool(ours and ours.founding)
        streak_ok = founding_now or bool(ours and ours.founding_streak_ok)
        status = ours.status if ours else None
        events: list[SubscriptionEvent] = []
        if ps.status == "active":
            if status in (None, SubscriptionStatus.LAPSED, SubscriptionStatus.GRACE):
                events.append(SubscriptionEvent.GRANTED)
                fields.update(founding=founding, founding_streak_ok=streak_ok)
            elif ps.scheduled_cancel:
                if status is not SubscriptionStatus.CANCEL_AT_PERIOD_END:
                    events.append(SubscriptionEvent.CANCEL_SCHEDULED)
            elif status is SubscriptionStatus.CANCEL_AT_PERIOD_END:
                events.append(SubscriptionEvent.CANCEL_REVOKED)
            else:
                events.append(SubscriptionEvent.PAYMENT_SUCCEEDED)
            if ps.scheduled_cancel and SubscriptionEvent.GRANTED in events:
                events.append(SubscriptionEvent.CANCEL_SCHEDULED)
        elif ps.status == "past_due":
            events.append(SubscriptionEvent.PAYMENT_FAILED)
        elif ps.status in ("canceled", "paused"):
            if status is None:
                return (
                    BillingEventOutcome.IGNORED_STALE,
                    f"{ps.status} before any activation",
                )
            if status is SubscriptionStatus.PAST_DUE:
                events.append(SubscriptionEvent.PAST_DUE_CEILING)
            else:
                events.append(SubscriptionEvent.TERM_ENDED)
                end = (
                    as_aware_utc(ps.canceled_at)
                    or as_aware_utc(ours.current_period_end if ours else None)
                    or now
                )
                fields["grace_until"] = max(now, end) + timedelta(days=GRACE_DAYS)
        else:
            return BillingEventOutcome.IGNORED_NOOP, f"provider status {ps.status}"

        events = [
            e for e in events if not (e in _STATE_ONLY_EVENTS and is_noop(status, e))
        ]
        if not events:
            return BillingEventOutcome.IGNORED_NOOP, "no state change"
        return await self._transition_all(user_id, events, event, fields)

    async def _apply_transaction(
        self, event: WebhookEvent
    ) -> tuple[BillingEventOutcome, str | None]:
        tx = await self._provider.fetch_transaction(event.data["id"])
        if tx.subscription_id:
            return BillingEventOutcome.IGNORED_NOOP, "subscription payment"
        if tx.status != "completed":
            return BillingEventOutcome.IGNORED_NOOP, f"transaction {tx.status}"
        year_price = self._settings.paddle_price_pro_year
        if not year_price or year_price not in tx.price_ids:
            log.error(
                "billing_unknown_price", transaction_id=tx.id, prices=tx.price_ids
            )
            return BillingEventOutcome.IGNORED_UNKNOWN, "no known price on transaction"
        user_id = _user_id(tx.custom_data)
        if user_id is None:
            log.error(
                "billing_unknown_user", transaction_id=tx.id, event_id=event.event_id
            )
            return BillingEventOutcome.IGNORED_UNKNOWN, "no user on transaction"

        ours = await self._subs.find_by_user(user_id)
        if ours is not None and ours.provider_ids.get("last_transaction_id") == tx.id:
            return BillingEventOutcome.IGNORED_NOOP, "transaction already applied"
        now = datetime.now(timezone.utc)
        base = now
        provider_ids = {**(ours.provider_ids if ours else {})}
        to_cancel = None
        if ours is not None and ours.effective_plan is Plan.PRO:
            base = max(now, as_aware_utc(ours.term_end) or now)
            if (
                ours.kind is SubscriptionKind.RECURRING
                and ours.status is not SubscriptionStatus.CANCEL_AT_PERIOD_END
            ):
                to_cancel = ours.provider_ids.get("subscription_id")
        if tx.customer_id:
            provider_ids["customer_id"] = tx.customer_id
        provider_ids["last_transaction_id"] = tx.id
        founding_now = self._founding_discount_applied(tx.discount_id)
        if to_cancel:
            # Written before the provider call: a failed cancel is retried by
            # the lifecycle reconcile until it lands.
            provider_ids["cancel_pending"] = to_cancel
        fields = {
            "provider": SubscriptionProvider.PADDLE,
            "kind": SubscriptionKind.PREPAID,
            "provider_ids": provider_ids,
            "prepaid_until": base + timedelta(days=PREPAID_DAYS),
            "founding": founding_now or bool(ours and ours.founding),
            "founding_streak_ok": founding_now
            or bool(ours and ours.founding_streak_ok),
        }
        outcome = await self._transition_all(
            user_id, [SubscriptionEvent.GRANTED], event, fields
        )
        if to_cancel and outcome[0] is BillingEventOutcome.APPLIED:
            await self.schedule_pending_cancel(user_id, to_cancel)
        return outcome

    async def schedule_pending_cancel(self, user_id: ObjectId, sub_id: str) -> bool:
        return await schedule_pending_cancel(
            self._provider, self._subs, user_id, sub_id
        )

    def _founding_discount_applied(self, discount_id: str | None) -> bool:
        """Founding is what the provider applied, never what checkout intended."""
        s = self._settings
        founding_ids = {
            s.paddle_discount_founding_first_monthly,
            s.paddle_discount_founding_first_year,
            s.paddle_discount_founding_renew_monthly,
            s.paddle_discount_founding_renew_year,
        } - {""}
        return discount_id in founding_ids

    async def _apply_adjustment(
        self, event: WebhookEvent
    ) -> tuple[BillingEventOutcome, str | None]:
        adj = await self._provider.fetch_adjustment(event.data["id"])
        if not (
            adj.action in ("refund", "chargeback")
            and adj.type == "full"
            and adj.status == "approved"
        ):
            return (
                BillingEventOutcome.IGNORED_NOOP,
                f"{adj.action} {adj.type} {adj.status}",
            )
        tx = await self._provider.fetch_transaction(adj.transaction_id)
        user_id = _user_id(tx.custom_data)
        if user_id is None and adj.subscription_id:
            ours = await self._subs.find_by_provider_subscription(adj.subscription_id)
            user_id = ours.user_id if ours else None
        if user_id is None:
            log.error(
                "billing_unknown_user", adjustment_id=adj.id, event_id=event.event_id
            )
            return BillingEventOutcome.IGNORED_UNKNOWN, "no user for refund"
        return await self._transition_all(user_id, [SubscriptionEvent.ENDED], event, {})

    async def _transition_all(
        self,
        user_id: ObjectId,
        events: list[SubscriptionEvent],
        event: WebhookEvent,
        fields: dict[str, Any],
    ) -> tuple[BillingEventOutcome, str | None]:
        applied: list[str] = []
        for sub_event in events:
            try:
                await self._entitlements.transition(
                    user_id,
                    sub_event,
                    actor="paddle",
                    reason=f"{event.event_type} {event.event_id}",
                    **fields,
                )
            except InvalidTransitionError as exc:
                log.warning(
                    "billing_webhook_stale",
                    event_id=event.event_id,
                    sub_event=sub_event.value,
                    detail=str(exc),
                )
                if applied:
                    return BillingEventOutcome.APPLIED, f"{','.join(applied)}; {exc}"
                return BillingEventOutcome.IGNORED_STALE, str(exc)
            applied.append(sub_event.value)
            fields = {}
        return BillingEventOutcome.APPLIED, ",".join(applied)


async def schedule_pending_cancel(
    provider: BillingProvider,
    subs: SubscriptionRepository,
    user_id: ObjectId,
    sub_id: str,
) -> bool:
    """Cancel the recurring subscription a prepaid year replaced. Clears
    ``cancel_pending`` once the provider shows it canceled or scheduled, and
    leaves it for the next attempt when the provider refuses. The webhook
    handler and the lifecycle tick both retry through here."""
    try:
        remote = await provider.fetch_subscription(sub_id)
        if remote.status != "canceled" and not remote.scheduled_cancel:
            await provider.schedule_cancel(sub_id)
    except BillingProviderError as exc:
        log.error(
            "billing_schedule_cancel_failed",
            subscription_id=sub_id,
            user_id=str(user_id),
            error=str(exc),
        )
        return False
    await subs.clear_cancel_pending(user_id)
    return True


def _user_id(custom_data: dict[str, Any]) -> ObjectId | None:
    raw = custom_data.get("user_id") if custom_data else None
    if isinstance(raw, str) and ObjectId.is_valid(raw):
        return ObjectId(raw)
    return None


def _holds_a_term(sub: SubscriptionDoc) -> bool:
    """True while the provider is still billing, or a prepaid term is live.
    Grace is not a term: the banner there tells the customer to buy again."""
    if sub.kind is SubscriptionKind.RECURRING:
        return sub.status in _LIVE_RECURRING
    return sub.status is SubscriptionStatus.ACTIVE


def _by_cadence(monthly: str, year: str, cadence: str) -> str | None:
    return (year if cadence == "year" else monthly) or None


def _subscription_ref(event: WebhookEvent) -> str | None:
    data = event.data
    if event.event_type.startswith("subscription."):
        return data.get("id")
    return data.get("subscription_id")


def _transaction_ref(event: WebhookEvent) -> str | None:
    data = event.data
    if event.event_type.startswith("transaction."):
        return data.get("id")
    return data.get("transaction_id")
