"""Account erasure cascade — GDPR Art. 17 hard delete.

One class, two entry points: ``erase()`` removes a single account from
every collection and external system that references it; ``sweep()``
drains the purge queue (the users collection itself — PENDING_DELETION
docs past their ``purge_after``).

The cascade order is load-bearing: erasure starts with an atomic claim
(PENDING_DELETION/ERASING + purge-due → ERASING, so a grace-period
restore that lands mid-sweep wins and the account survives), ``email``
and the R2 owner-key prefix are captured BEFORE anything is deleted,
external systems (R2, PostHog) run before the user doc dies, the users
collection goes LAST — so a crash anywhere re-queues the whole user on
the next sweep — and the confirmation mail (the one non-idempotent step)
fires only AFTER the doc is gone. That makes at-least-once execution
safe: every other step is a ``delete_many``/``$pull`` that no-ops on
re-run.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Protocol

from errors import R2StorageError
from infrastructure.logging import get_logger
from services.image_ingest import owner_key_prefix
from services.scheduler.registry import ScheduledTask

if TYPE_CHECKING:
    from bson import ObjectId

    from infrastructure.storage.r2 import R2StorageClient
    from repositories.api_key_repository import ApiKeyRepository
    from repositories.app_grant_repository import AppGrantRepository
    from repositories.click_repository import ClickRepository
    from repositories.entitlement_event_repository import (
        EntitlementEventRepository,
    )
    from repositories.entitlement_override_repository import (
        EntitlementOverrideRepository,
    )
    from repositories.feature_flag_repository import FeatureFlagRepository
    from repositories.page_layout_repository import PageLayoutRepository
    from repositories.report_repository import (
        ReportRepository,
        ReportSubmissionRepository,
    )
    from repositories.subscription_repository import SubscriptionRepository
    from repositories.token_repository import TokenRepository
    from repositories.user_repository import UserRepository
    from repositories.webhook_delivery_repository import WebhookDeliveryRepository
    from repositories.webhook_endpoint_repository import WebhookEndpointRepository
    from repositories.webhook_event_repository import WebhookEventRepository
    from services.custom_domain_service import CustomDomainService
    from services.tag_service import TagService
    from services.url_service import UrlService

log = get_logger(__name__)

# One $in per click delete — bounded so an account with six figures of links
# can never push a 16MB query document at the time-series collection.
_CLICK_ID_CHUNK = 500


class PostHogEraser(Protocol):
    """Deletes a PostHog person (and their events) by distinct_id."""

    async def delete_person(self, distinct_id: str) -> None: ...


class NoopPostHogEraser:
    """Default when PostHog erasure is unconfigured — nothing to erase."""

    async def delete_person(self, distinct_id: str) -> None:
        return None


class ErasureMailer(Protocol):
    """Deletion lifecycle mail: the grace-period notice (carrying the
    one-shot cancel link when a restore token was minted), the "deletion
    cancelled" notice on restore, and the final "your account was erased"
    confirmation. Returns whether the mail was sent — callers treat all
    three as best-effort and ignore it."""

    async def send_deletion_requested(
        self, email: str, purge_after: datetime, restore_token: str | None = None
    ) -> bool: ...

    async def send_deletion_cancelled(self, email: str) -> bool: ...

    async def send_erasure_confirmation(self, email: str) -> bool: ...


class NoopErasureMailer:
    """Default when ZeptoMail is unconfigured — nothing gets sent."""

    async def send_deletion_requested(
        self, email: str, purge_after: datetime, restore_token: str | None = None
    ) -> bool:
        return False

    async def send_deletion_cancelled(self, email: str) -> bool:
        return False

    async def send_erasure_confirmation(self, email: str) -> bool:
        return False


class AccountErasureService:
    """Hard-erases one account across every collection that references it.

    Every delete step is idempotent (``delete_many`` / ``$pull``); the user
    doc goes last and the confirmation mail only after it, so at-least-once
    sweep execution is safe: a crash anywhere re-queues the user (still
    ERASING, still purge-due) for the next sweep.
    """

    def __init__(
        self,
        *,
        # Keyword-only, repos included: a mis-ordered positional repo in a
        # destructive cascade fails silently; this makes drift a type error.
        user_repo: UserRepository,
        url_service: UrlService,
        domain_service: CustomDomainService,
        tag_service: TagService,
        click_repo: ClickRepository,
        api_key_repo: ApiKeyRepository,
        token_repo: TokenRepository,
        page_layout_repo: PageLayoutRepository,
        app_grant_repo: AppGrantRepository,
        webhook_endpoint_repo: WebhookEndpointRepository,
        webhook_event_repo: WebhookEventRepository,
        webhook_delivery_repo: WebhookDeliveryRepository,
        report_repo: ReportRepository,
        report_submission_repo: ReportSubmissionRepository,
        feature_flag_repo: FeatureFlagRepository,
        subscription_repo: SubscriptionRepository,
        override_repo: EntitlementOverrideRepository,
        entitlement_event_repo: EntitlementEventRepository,
        r2_storage: R2StorageClient | None = None,
        posthog: PostHogEraser | None = None,
        mailer: ErasureMailer | None = None,
        key_secret: str = "",
        batch_limit: int = 25,
        time_budget_seconds: float = 480,
        claim_lease_seconds: int = 900,
    ) -> None:
        self._user_repo = user_repo
        self._url_service = url_service
        self._domain_service = domain_service
        self._tag_service = tag_service
        self._click_repo = click_repo
        self._api_key_repo = api_key_repo
        self._token_repo = token_repo
        self._page_layout_repo = page_layout_repo
        self._app_grant_repo = app_grant_repo
        self._webhook_endpoint_repo = webhook_endpoint_repo
        self._webhook_event_repo = webhook_event_repo
        self._webhook_delivery_repo = webhook_delivery_repo
        self._report_repo = report_repo
        self._report_submission_repo = report_submission_repo
        self._feature_flag_repo = feature_flag_repo
        self._subscription_repo = subscription_repo
        self._override_repo = override_repo
        self._entitlement_event_repo = entitlement_event_repo
        # None ⇒ no R2 on this deployment; the sweep step counts 0.
        self._r2_storage = r2_storage
        self._posthog = posthog or NoopPostHogEraser()
        self._mailer = mailer or NoopErasureMailer()
        # HMAC pepper for R2 owner prefixes — same secret the upload paths
        # key with (settings.secret_key), or the sweep misses everything.
        self._key_secret = key_secret
        self._batch_limit = batch_limit
        # Stop STARTING erasures once this much of a sweep run is spent —
        # default 80% of the scheduler's 600s lease (see sweep()).
        self._time_budget_seconds = time_budget_seconds
        # How long a claim marks a cascade LIVE (not stealable) — must exceed
        # the time budget plus one heavy cascade or runners double-erase.
        self._claim_lease_seconds = claim_lease_seconds

    async def erase(self, user_id: ObjectId) -> dict[str, int]:
        """Run the full cascade for one account. Returns per-step counts.

        Starts with the atomic erasure claim (guarded flip to ERASING):
        a failed claim returns ``{}`` — the account was restored mid-sweep,
        already erased, or is not purge-due — and nothing is touched. Once
        the claim holds, erasure is final: restore no longer matches.
        Mongo/R2 failures propagate (the ERASING doc is still there, so a
        later sweep re-claims and retries once its claim lease expires);
        PostHog and mail failures are
        swallowed — neither may park an account in ERASING forever.
        Deleted R2 objects may outlive origin deletion at the CDN edge
        until their cache TTL lapses — no CDN purge is attempted here.
        """
        claimed = await self._user_repo.claim_for_erasure(
            user_id,
            now=datetime.now(timezone.utc),
            lease_seconds=self._claim_lease_seconds,
        )
        if not claimed:
            return {}
        user = await self._user_repo.find_by_id(user_id)
        if user is None:
            return {}
        # Captured BEFORE any deletion: the confirmation mail and the
        # email-keyed predicates need these after the docs are gone.
        email = user.email
        # Prefer the prefix pinned at upload time — the one the objects live
        # under across SECRET_KEY rotations; sweep BOTH when they diverge.
        computed_prefix = owner_key_prefix(user_id, self._key_secret)
        stored_prefix = user.storage_prefix
        if stored_prefix and stored_prefix != computed_prefix:
            r2_prefixes: tuple[str, ...] = (stored_prefix, computed_prefix)
        else:
            r2_prefixes = (stored_prefix or computed_prefix,)

        counts: dict[str, int] = {}
        # BLOCKED links come back retained + scrubbed, never deleted —
        # Art. 17(3) abuse retention (audit trail + alias reservation).
        url_result = await self._url_service.delete_all_by_owner(user_id)
        counts["urlsV2"] = url_result.deleted
        counts["urls_blocked_retained"] = url_result.blocked_retained
        counts["custom_domains"] = await self._domain_service.delete_all_for_owner(
            user_id
        )
        counts["tags"] = await self._tag_service.delete_all_for_owner(user_id)
        # Clicks are time-series: both predicates must stay metaField-only.
        # url_id chunks first — pre-claim clicks carry the anonymous sentinel.
        clicks_deleted = 0
        for start in range(0, len(url_result.url_ids), _CLICK_ID_CHUNK):
            clicks_deleted += await self._click_repo.delete_by_url_ids(
                url_result.url_ids[start : start + _CLICK_ID_CHUNK]
            )
        # Owner-based mop-up: clicks on links deleted before erasure. The two
        # passes are physical deletes, so the sum can never double-count.
        clicks_deleted += await self._click_repo.delete_by_owner(user_id)
        counts["clicks"] = clicks_deleted
        # Satellites are hard deletes — soft-delete flags ignored.
        counts["api_keys"] = await self._api_key_repo.delete_by_user(user_id)
        counts["verification_tokens"] = await self._token_repo.delete_by_user_or_email(
            user_id, email
        )
        counts["page_layouts"] = await self._page_layout_repo.delete_by_user(user_id)
        counts["app_grants"] = await self._app_grant_repo.delete_by_user(user_id)
        counts["webhook_endpoints"] = await self._webhook_endpoint_repo.delete_by_user(
            user_id
        )
        counts["webhook_events"] = await self._webhook_event_repo.delete_by_owner(
            user_id
        )
        counts["webhook_deliveries"] = await self._webhook_delivery_repo.delete_by_user(
            user_id
        )
        counts[
            "report_submissions"
        ] = await self._report_submission_repo.delete_by_reporter(user_id, email)
        # $pull, not delete — the shared docs stay, the user's identifiers go.
        counts["reports_pulled"] = await self._report_repo.pull_reporter(user_id)
        counts["feature_flags_pulled"] = await self._feature_flag_repo.pull_allowlisted(
            user_id, email
        )
        counts["subscriptions"] = await self._subscription_repo.delete_by_user(user_id)
        counts["entitlement_overrides"] = await self._override_repo.delete_by_user(
            user_id
        )
        counts[
            "entitlement_events"
        ] = await self._entitlement_event_repo.delete_by_user(user_id)
        counts["r2_objects"] = await self._sweep_r2(r2_prefixes)
        # External systems, then the doc itself — LAST among deletes.
        await self._erase_posthog(user_id)
        deleted = await self._user_repo.delete_hard(user_id)
        # The mail is the one non-idempotent step: only after the doc is gone,
        # or a failing delete_hard resends "permanently deleted" every sweep.
        if deleted:
            await self._send_confirmation(email)
        else:
            log.warning("account_erase_doc_already_gone", user_id=str(user_id))

        # The one compliance record — user_id + counts, never email/IP.
        log.info("account_erased", user_id=str(user_id), **counts)
        return counts

    async def sweep(self) -> dict[str, int]:
        """Erase every account whose purge deadline has passed.

        Per-user failures are isolated: log, count, continue — the failed
        user stays claimable and the next sweep retries. The batch limit
        bounds one run; the time budget stops STARTING new erasures once a
        run has outstayed its welcome (heavy cascades can outrun the
        scheduler lease — the in-flight one finishes, the rest defer to the
        next sweep). The recurring schedule drains any backlog.
        """
        started = time.monotonic()
        due = await self._user_repo.find_purge_due(
            now=datetime.now(timezone.utc),
            limit=self._batch_limit,
            lease_seconds=self._claim_lease_seconds,
        )
        erased = failed = skipped = deferred = 0
        for index, user in enumerate(due):
            if time.monotonic() - started >= self._time_budget_seconds:
                deferred = len(due) - index
                log.warning(
                    "account_erasure_sweep_budget_exhausted",
                    deferred=deferred,
                    budget_seconds=self._time_budget_seconds,
                )
                break
            try:
                counts = await self.erase(user.id)
            except Exception:
                log.exception("account_erase_failed", user_id=str(user.id))
                failed += 1
            else:
                if counts:
                    erased += 1
                else:
                    # Claim lost — restored mid-sweep or already purged.
                    skipped += 1
        return {
            "erased": erased,
            "failed": failed,
            "skipped": skipped,
            "deferred": deferred,
        }

    # ── Internal ────────────────────────────────────────────────────────

    async def _sweep_r2(self, prefixes: tuple[str, ...]) -> int:
        """Delete every stored object under the owner's key prefix(es).

        Usually one prefix; two when the pinned ``storage_prefix`` and the
        freshly computed HMAC diverge after a SECRET_KEY rotation — objects
        may sit under either. Rotations that predate the pinning field
        still orphan the old prefix (historical; needs the old secret).
        A failed delete RAISES — the user doc must survive so the next
        sweep retries, instead of orphaning the object forever.
        """
        if self._r2_storage is None or not self._r2_storage.is_configured:
            return 0
        removed = 0
        for prefix in prefixes:
            for scope in (f"profile-pictures/{prefix}/", f"og/{prefix}/"):
                for key in await self._r2_storage.list_keys(scope):
                    if not await self._r2_storage.delete_object(key):
                        raise R2StorageError(f"erasure could not delete {key}")
                    removed += 1
        return removed

    async def _erase_posthog(self, user_id: ObjectId) -> None:
        """Best-effort person deletion — log-and-continue on failure.

        The step is idempotent and PostHog re-runs free on the next sweep
        only if something ELSE fails after it; an analytics outage must
        never block the Mongo erasure.
        """
        try:
            await self._posthog.delete_person(str(user_id))
        except Exception as exc:
            log.warning(
                "account_erasure_posthog_failed",
                user_id=str(user_id),
                error=str(exc),
            )

    async def _send_confirmation(self, email: str) -> None:
        """Best-effort confirmation mail — log-and-continue on failure.

        Propagating would let a mail outage park accounts in
        PENDING_DELETION past the GDPR deadline — worse than a missed
        courtesy mail. Never log the address.
        """
        try:
            await self._mailer.send_erasure_confirmation(email)
        except Exception as exc:
            log.warning("account_erasure_mail_failed", error=str(exc))


# ── Scheduler registration ───────────────────────────────────────────────────

ERASURE_SWEEP_TASK = "account-erasure-sweep"
_ERASURE_SWEEP_CRON = "*/10 * * * *"


def erasure_sweep_task(service: AccountErasureService) -> ScheduledTask:
    """The sweep's scheduler registration — this module owns the name and
    cadence so the app wiring and the click worker can never drift. The
    10-minute cron keeps worst-case latency past ``purge_after`` small and
    re-drains any backlog the batch limit left behind."""
    return ScheduledTask(
        name=ERASURE_SWEEP_TASK, fn=service.sweep, schedule=_ERASURE_SWEEP_CRON
    )
