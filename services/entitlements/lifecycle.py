"""
The lifecycle job: every transition no webhook fires.

One tick a minute moves subscriptions on the clock (term end to grace, grace
end to lapsed, the past-due ceiling), sends the reminder, grace and lapsed
emails once each, and prunes expired overrides. Two daily tasks reconcile
against the billing provider and check that nothing the pro plan promises
is switched off by a forgotten flag.

Idempotent by construction: every transition re-reads state, and every email
is keyed on ``(user, kind, period)`` in the audit log and driven off the
status a subscription is in, not off who moved it there, so a webhook that
beat the clock or a mailer that failed once still gets its email sent. One
bad subscription is logged and skipped, never the whole tick. The tick's log
line is the heartbeat an Axiom monitor watches.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, TypeVar

from infrastructure.logging import get_logger
from repositories.custom_domain_repository import CustomDomainRepository
from repositories.entitlement_event_repository import EntitlementEventRepository
from repositories.entitlement_override_repository import (
    EntitlementOverrideRepository,
)
from repositories.feature_flag_repository import FeatureFlagRepository
from repositories.subscription_repository import SubscriptionRepository
from repositories.user_repository import UserRepository
from schemas.enums.domain_status import DomainStatus
from schemas.enums.rollout_type import RolloutType
from schemas.models.entitlement_event import EntitlementEventDoc, EntitlementEventKind
from schemas.models.feature_flag import FeatureFlagDoc
from schemas.models.subscription import (
    SubscriptionDoc,
    SubscriptionKind,
    SubscriptionStatus,
)
from services.billing.port import BillingProvider
from services.billing.service import schedule_pending_cancel
from services.entitlements.service import EntitlementService
from services.entitlements.state_machine import SubscriptionEvent
from services.entitlements.terms import GRACE_DAYS, PAST_DUE_CEILING_DAYS
from services.features.catalog import FEATURES, Kind, Plan
from services.scheduler.registry import ScheduledTask
from shared.datetime_utils import as_aware_utc

log = get_logger(__name__)

LIFECYCLE_TASK = "entitlements-lifecycle"
RECONCILE_TASK = "billing-reconcile"
PRO_VALUE_TASK = "entitlements-pro-value-check"

REMINDER_DAYS = (30, 7, 1)
# How far back a tick looks for lapses still owed a mail and override writes
# still owed a reconcile; both are idempotent, so overlap is harmless.
_CATCH_UP = timedelta(days=30)
_OVERRIDE_CATCH_UP = timedelta(minutes=2)
_ACTOR = "lifecycle"
T = TypeVar("T")
# Paddle status a subscription in each of our statuses should show.
_EXPECTED_PROVIDER_STATUS = {
    SubscriptionStatus.ACTIVE: {"active", "trialing"},
    SubscriptionStatus.PAST_DUE: {"past_due"},
    SubscriptionStatus.CANCEL_AT_PERIOD_END: {"active"},
    SubscriptionStatus.GRACE: {"canceled"},
}


class LifecycleMailer(Protocol):
    async def send_html(
        self,
        to_email: str,
        subject: str,
        template_name: str,
        context: dict,
        text_body: str,
    ) -> bool: ...


def _date(dt: datetime | None) -> str:
    aware = as_aware_utc(dt)
    return f"{aware:%B} {aware.day}, {aware.year}" if aware else ""


async def _each(
    items: Iterable[T], step: Callable[[T], Awaitable[bool]], *, what: str
) -> tuple[int, int]:
    """Run ``step`` per item; one failure is logged and counted, never raised."""
    done = failed = 0
    for item in items:
        try:
            done += int(await step(item))
        except Exception:
            failed += 1
            log.exception("entitlements_lifecycle_item_failed", step=what)
    return done, failed


class LifecycleService:
    def __init__(
        self,
        entitlements: EntitlementService,
        subscriptions: SubscriptionRepository,
        overrides: EntitlementOverrideRepository,
        events: EntitlementEventRepository,
        users: UserRepository,
        domains: CustomDomainRepository,
        flags: FeatureFlagRepository,
        mailer: LifecycleMailer | None,
        *,
        app_url: str,
        provider: BillingProvider | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._entitlements = entitlements
        self._subs = subscriptions
        self._overrides = overrides
        self._events = events
        self._users = users
        self._domains = domains
        self._flags = flags
        self._mailer = mailer
        self._app_url = app_url.rstrip("/")
        self._provider = provider
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # ── The minute tick ──────────────────────────────────────────────────

    async def tick(self) -> dict[str, Any]:
        now = self._clock()
        counts: dict[str, int] = {"failed": 0}
        phases = (
            ("grace_started", self._term_endings),
            ("lapsed", self._grace_endings),
            ("past_due_lapsed", self._past_due_ceiling),
            ("grace_mails", self._grace_mails),
            ("lapsed_mails", self._lapsed_mails),
            ("reminders", self._reminders),
            ("overrides_pruned", self._prune_overrides),
            ("overrides_reconciled", self._reconcile_override_writes),
            ("cancels_scheduled", self._pending_cancels),
        )
        try:
            for name, phase in phases:
                try:
                    done, failed = await phase(now)
                except Exception:
                    done, failed = 0, 1
                    log.exception("entitlements_lifecycle_phase_failed", phase=name)
                counts[name] = done
                counts["failed"] += failed
        finally:
            log.info("entitlements_lifecycle_tick", **counts)
        return counts

    async def _term_endings(self, now: datetime) -> tuple[int, int]:
        async def step(sub: SubscriptionDoc) -> bool:
            end = as_aware_utc(sub.term_end) or now
            after = await self._entitlements.transition(
                sub.user_id,
                SubscriptionEvent.TERM_ENDED,
                actor=_ACTOR,
                reason="paid term ended",
                now=now,
                grace_until=max(now, end) + timedelta(days=GRACE_DAYS),
            )
            return after.status is SubscriptionStatus.GRACE

        return await _each(await self._subs.find_term_ended(now), step, what="term")

    async def _grace_endings(self, now: datetime) -> tuple[int, int]:
        async def step(sub: SubscriptionDoc) -> bool:
            after = await self._entitlements.transition(
                sub.user_id,
                SubscriptionEvent.GRACE_ENDED,
                actor=_ACTOR,
                reason="grace period ended",
                now=now,
            )
            return after.status is SubscriptionStatus.LAPSED

        return await _each(await self._subs.find_grace_ended(now), step, what="grace")

    async def _past_due_ceiling(self, now: datetime) -> tuple[int, int]:
        cutoff = now - timedelta(days=PAST_DUE_CEILING_DAYS)

        async def step(sub: SubscriptionDoc) -> bool:
            after = await self._entitlements.transition(
                sub.user_id,
                SubscriptionEvent.PAST_DUE_CEILING,
                actor=_ACTOR,
                reason=f"past due for {PAST_DUE_CEILING_DAYS} days after the period end",
                now=now,
            )
            return after.status is SubscriptionStatus.LAPSED

        return await _each(
            await self._subs.find_past_due_older_than(cutoff), step, what="past_due"
        )

    async def _grace_mails(self, now: datetime) -> tuple[int, int]:
        async def step(sub: SubscriptionDoc) -> bool:
            grace_until = as_aware_utc(sub.grace_until) or now
            return await self._mail_once(
                sub,
                f"grace:{grace_until.date()}",
                now,
                subject=f"Your Pro term has ended; you keep Pro for {GRACE_DAYS} more days",
                template="plan_grace_started.html",
                context={"grace_date": _date(grace_until), "grace_days": GRACE_DAYS},
                text=(
                    "Your paid term has ended. You keep every Pro feature until "
                    f"{_date(grace_until)}. After that the account goes back to Free. "
                    "Nothing is deleted."
                ),
            )

        return await _each(await self._subs.find_in_grace(), step, what="grace_mail")

    async def _lapsed_mails(self, now: datetime) -> tuple[int, int]:
        async def step(sub: SubscriptionDoc) -> bool:
            lapsed_at = as_aware_utc(sub.updated_at) or now
            return await self._mail_once(
                sub,
                f"lapsed:{lapsed_at.date()}",
                now,
                subject="Your spoo.me account is back on Free",
                template="plan_lapsed.html",
                context={"lapsed_date": _date(lapsed_at)},
                text=(
                    f"Your Pro access ended on {_date(lapsed_at)}. Every link and all "
                    "of your analytics are still here; Pro-only settings switch back "
                    f"on when you renew at {self._app_url}/upgrade."
                ),
            )

        # A refund or a manual end is not a term running out: no renewal mail.
        lapsed = await self._subs.find_lapsed_since(
            now - _CATCH_UP, not_by=SubscriptionEvent.ENDED.value
        )
        return await _each(lapsed, step, what="lapsed_mail")

    async def _reminders(self, now: datetime) -> tuple[int, int]:
        horizon = now + timedelta(days=max(REMINDER_DAYS) + 1)

        async def step(sub: SubscriptionDoc) -> bool:
            end = as_aware_utc(sub.term_end)
            if end is None or end <= now:
                return False
            days_left = (end - now).days or 1
            due = [d for d in REMINDER_DAYS if days_left <= d]
            if not due:
                return False
            # The nearest threshold that has passed is the one to send; earlier
            # ones that were missed are folded into it rather than sent late.
            threshold = min(due)
            scheduled_cancel = sub.kind is SubscriptionKind.RECURRING
            action = (
                "To stay on Pro, undo the cancellation from Billing in your dashboard "
                f"settings: {self._app_url}/dashboard/settings"
                if scheduled_cancel
                else f"Renew at {self._app_url}/upgrade to stay on Pro."
            )
            return await self._mail_once(
                sub,
                f"term:{end.date()}:{threshold}",
                now,
                subject=f"Your spoo.me Pro term ends in {days_left} "
                f"{'day' if days_left == 1 else 'days'}",
                template="plan_reminder.html",
                context={
                    "end_date": _date(end),
                    "days_left": days_left,
                    "grace_days": GRACE_DAYS,
                    "scheduled_cancel": scheduled_cancel,
                },
                text=(
                    f"Your spoo.me Pro term ends on {_date(end)}. After that you keep "
                    f"Pro for {GRACE_DAYS} more days, then the account goes back to "
                    f"Free. {action}"
                ),
            )

        return await _each(
            await self._subs.find_terms_ending_before(horizon), step, what="reminder"
        )

    async def _prune_overrides(self, now: datetime) -> tuple[int, int]:
        async def step(override) -> bool:
            revoked = await self._overrides.revoke_expired(
                override.user_id, override.key, now, actor=_ACTOR, reason="expired"
            )
            if revoked:
                await self._entitlements.reconcile_over_limit(override.user_id)
            return revoked

        return await _each(
            await self._overrides.find_expired(now), step, what="prune_override"
        )

    async def _reconcile_override_writes(self, now: datetime) -> tuple[int, int]:
        """Overrides are also written outside this process (ops tooling); the
        pause policy catches up with them here."""

        async def step(user_id) -> bool:
            await self._entitlements.reconcile_over_limit(user_id)
            return True

        users = await self._events.users_with_override_writes_since(
            now - _OVERRIDE_CATCH_UP
        )
        return await _each(users, step, what="override_reconcile")

    async def _pending_cancels(self, now: datetime) -> tuple[int, int]:
        """A prepaid year that could not cancel the monthly it replaced keeps
        ``cancel_pending`` until the provider accepts the cancel."""
        if self._provider is None or self._provider.name == "none":
            return 0, 0

        async def step(sub: SubscriptionDoc) -> bool:
            return await schedule_pending_cancel(
                self._provider,
                self._subs,
                sub.user_id,
                sub.provider_ids["cancel_pending"],
            )

        return await _each(
            await self._subs.find_cancel_pending(), step, what="pending_cancel"
        )

    # ── Emails ───────────────────────────────────────────────────────────

    async def _mail_once(
        self,
        sub: SubscriptionDoc,
        period: str,
        now: datetime,
        *,
        subject: str,
        template: str,
        context: dict[str, Any],
        text: str,
    ) -> bool:
        kind = EntitlementEventKind.REMINDER_SENT
        if self._mailer is None:
            return False
        if await self._events.has_reminder(sub.user_id, kind, period):
            return False
        user = await self._users.find_by_id(sub.user_id)
        if user is None or not user.email:
            return False
        domains = [
            d.fqdn
            for d in await self._domains.list_live_by_owner(sub.user_id)
            if d.status is DomainStatus.ACTIVE
        ]
        if domains:
            text += (
                f"\n\nLinks on {', '.join(domains)} stop resolving when Pro ends; "
                "the domain settings and links stay saved."
            )
        text = f"Hello,\n\n{text}\n\nSupport Team, spoo.me"
        try:
            sent = await self._mailer.send_html(
                user.email, subject, template, {**context, "domains": domains}, text
            )
        except Exception as exc:
            log.warning(
                "entitlements_lifecycle_mail_failed", kind=kind.value, error=str(exc)
            )
            return False
        if not sent:
            return False
        # The audit append is the dedupe key; reminders do not bump the version.
        await self._events.append(
            EntitlementEventDoc(
                user_id=sub.user_id,
                kind=kind,
                actor=_ACTOR,
                reason=subject,
                period=period,
                at=now,
            )
        )
        return True

    # ── Daily tasks ──────────────────────────────────────────────────────

    async def reconcile(self) -> dict[str, Any]:
        """Compare every live provider subscription with the provider. Logs
        drift; never changes a status, and a failed lookup is just a log line."""
        if self._provider is None or self._provider.name == "none":
            return {"skipped": True}
        checked = drift = failed = 0
        for sub in await self._subs.find_provider_live(self._provider.name):
            sub_id = sub.provider_ids.get("subscription_id")
            if not sub_id:
                continue
            checked += 1
            try:
                remote = await self._provider.fetch_subscription(sub_id)
            except Exception as exc:
                failed += 1
                log.warning(
                    "subscription_reconcile_lookup_failed",
                    user_id=str(sub.user_id),
                    subscription_id=sub_id,
                    error=str(exc),
                )
                continue
            expected = _EXPECTED_PROVIDER_STATUS.get(sub.status, set())
            if remote.status not in expected:
                drift += 1
                log.warning(
                    "subscription_drift",
                    user_id=str(sub.user_id),
                    subscription_id=sub_id,
                    ours=sub.status.value,
                    provider=remote.status,
                    provider_scheduled_cancel=remote.scheduled_cancel,
                )
        counts = {"checked": checked, "drift": drift, "failed": failed}
        log.info("subscription_reconcile_completed", **counts)
        return counts

    async def pro_value_check(self) -> dict[str, Any]:
        """Catalog features the pro plan includes whose rollout is off: a
        forgotten kill switch would silently withhold something paid for."""
        withheld: list[str] = []
        for feature in FEATURES.values():
            if feature.kind is not Kind.BOOL or feature.rollout is None:
                continue
            if not feature.plans[Plan.PRO] or feature.plans[Plan.FREE]:
                continue
            flag = await self._flags.find_by_name(feature.rollout)
            if flag is None or not _reaches_anyone(flag):
                withheld.append(feature.key)
        if withheld:
            log.warning("pro_value_withheld", features=withheld)
        return {"withheld": withheld}


def _reaches_anyone(flag: FeatureFlagDoc) -> bool:
    if not flag.enabled or flag.rollout_type is RolloutType.OFF:
        return False
    if flag.rollout_type is RolloutType.ALLOWLIST:
        return bool(flag.allowlist_user_ids or flag.allowlist_emails)
    if flag.rollout_type is RolloutType.PERCENTAGE:
        return flag.percentage > 0
    if flag.rollout_type is RolloutType.HEX_DIGIT:
        return bool(flag.enabled_digits)
    return True


def lifecycle_tasks(service: LifecycleService) -> list[ScheduledTask]:
    """This module owns the names and cadences so the app and the worker
    register identical tasks."""
    return [
        ScheduledTask(name=LIFECYCLE_TASK, fn=service.tick, schedule="* * * * *"),
        ScheduledTask(name=RECONCILE_TASK, fn=service.reconcile, schedule="0 4 * * *"),
        ScheduledTask(
            name=PRO_VALUE_TASK, fn=service.pro_value_check, schedule="30 4 * * *"
        ),
    ]
