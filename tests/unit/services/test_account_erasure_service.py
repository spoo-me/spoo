"""Unit tests for AccountErasureService."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/")

from bson import ObjectId

from errors import R2StorageError
from services.account_erasure_service import (
    ERASURE_SWEEP_TASK,
    AccountErasureService,
    NoopErasureMailer,
    NoopPostHogEraser,
    erasure_sweep_task,
)
from services.image_ingest import owner_key_prefix
from services.scheduler.tasks import build_task_registry
from services.url_service import OwnerUrlErasure

UID = ObjectId("aaaaaaaaaaaaaaaaaaaaaaaa")
UID2 = ObjectId("bbbbbbbbbbbbbbbbbbbbbbbb")
EMAIL = "user@example.com"
SECRET = "test-secret"
URL_IDS = [ObjectId("cccccccccccccccccccccccc"), ObjectId("dddddddddddddddddddddddd")]


def _user_doc(user_id=UID, email=EMAIL, storage_prefix=None):
    doc = MagicMock()
    doc.id = user_id
    doc.email = email
    doc.storage_prefix = storage_prefix
    return doc


class Stubs:
    """Every injected dependency, with call-order recording.

    ``calls`` collects ``(name, args)`` tuples in await order so tests can
    assert the load-bearing cascade order (user doc LAST).
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []

        self.user_repo = MagicMock()
        self.user_repo.claim_for_erasure = self._tracked("users.claim", True)
        self.user_repo.find_by_id = AsyncMock(return_value=_user_doc())
        self.user_repo.delete_hard = self._tracked("users.delete_hard", True)
        self.user_repo.find_purge_due = AsyncMock(return_value=[])

        self.url_service = MagicMock()
        self.url_service.delete_all_by_owner = self._tracked(
            "urls",
            OwnerUrlErasure(deleted=3, blocked_retained=1, url_ids=list(URL_IDS)),
        )

        self.domain_service = MagicMock()
        self.domain_service.delete_all_for_owner = self._tracked("domains", 1)

        self.tag_service = MagicMock()
        self.tag_service.delete_all_for_owner = self._tracked("tags", 2)

        self.click_repo = MagicMock()
        self.click_repo.delete_by_url_ids = self._tracked("clicks.by_url_ids", 4)
        self.click_repo.delete_by_owner = self._tracked("clicks", 2)

        self.api_key_repo = MagicMock()
        self.api_key_repo.delete_by_user = self._tracked("api_keys", 1)

        self.token_repo = MagicMock()
        self.token_repo.delete_by_user_or_email = self._tracked("tokens", 4)

        self.page_layout_repo = MagicMock()
        self.page_layout_repo.delete_by_user = self._tracked("page_layouts", 1)

        self.app_grant_repo = MagicMock()
        self.app_grant_repo.delete_by_user = self._tracked("app_grants", 2)

        self.webhook_endpoint_repo = MagicMock()
        self.webhook_endpoint_repo.delete_by_user = self._tracked(
            "webhook_endpoints", 1
        )
        self.webhook_event_repo = MagicMock()
        self.webhook_event_repo.delete_by_owner = self._tracked("webhook_events", 5)
        self.webhook_delivery_repo = MagicMock()
        self.webhook_delivery_repo.delete_by_user = self._tracked(
            "webhook_deliveries", 6
        )

        self.report_submission_repo = MagicMock()
        self.report_submission_repo.delete_by_reporter = self._tracked(
            "report_submissions", 1
        )
        self.report_repo = MagicMock()
        self.report_repo.pull_reporter = self._tracked("reports_pull", 2)

        self.feature_flag_repo = MagicMock()
        self.feature_flag_repo.pull_allowlisted = self._tracked("feature_flags_pull", 1)
        self.subscription_repo = MagicMock()
        self.subscription_repo.delete_by_user = self._tracked("subscriptions", 1)
        self.override_repo = MagicMock()
        self.override_repo.delete_by_user = self._tracked("entitlement_overrides", 2)
        self.entitlement_event_repo = MagicMock()
        self.entitlement_event_repo.delete_by_user = self._tracked(
            "entitlement_events", 3
        )

        prefix = owner_key_prefix(UID, SECRET)
        self.r2_storage = MagicMock()
        self.r2_storage.is_configured = True
        self.r2_keys = {
            f"profile-pictures/{prefix}/": [f"profile-pictures/{prefix}/a.png"],
            f"og/{prefix}/": [f"og/{prefix}/b.png"],
        }
        self.r2_storage.list_keys = AsyncMock(
            side_effect=lambda p: self._record("r2.list", (p,), self.r2_keys.get(p, []))
        )
        self.r2_storage.delete_object = AsyncMock(
            side_effect=lambda k: self._record("r2.delete", (k,), True)
        )

        self.posthog = MagicMock()
        self.posthog.delete_person = self._tracked("posthog", None)
        self.mailer = MagicMock()
        self.mailer.send_erasure_confirmation = self._tracked("mailer", None)

    def _record(self, name, args, result):
        self.calls.append((name, args))
        return result

    def _tracked(self, name, result):
        async def record(*args, **kwargs):
            self.calls.append((name, args))
            return result

        return AsyncMock(side_effect=record)

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def _service(
    stubs: Stubs,
    *,
    r2=...,
    posthog=...,
    mailer=...,
    batch_limit=25,
    time_budget_seconds=480,
    claim_lease_seconds=900,
):
    return AccountErasureService(
        user_repo=stubs.user_repo,
        url_service=stubs.url_service,
        domain_service=stubs.domain_service,
        tag_service=stubs.tag_service,
        click_repo=stubs.click_repo,
        api_key_repo=stubs.api_key_repo,
        token_repo=stubs.token_repo,
        page_layout_repo=stubs.page_layout_repo,
        app_grant_repo=stubs.app_grant_repo,
        webhook_endpoint_repo=stubs.webhook_endpoint_repo,
        webhook_event_repo=stubs.webhook_event_repo,
        webhook_delivery_repo=stubs.webhook_delivery_repo,
        report_repo=stubs.report_repo,
        report_submission_repo=stubs.report_submission_repo,
        feature_flag_repo=stubs.feature_flag_repo,
        subscription_repo=stubs.subscription_repo,
        override_repo=stubs.override_repo,
        entitlement_event_repo=stubs.entitlement_event_repo,
        r2_storage=stubs.r2_storage if r2 is ... else r2,
        posthog=stubs.posthog if posthog is ... else posthog,
        mailer=stubs.mailer if mailer is ... else mailer,
        key_secret=SECRET,
        batch_limit=batch_limit,
        time_budget_seconds=time_budget_seconds,
        claim_lease_seconds=claim_lease_seconds,
    )


EXPECTED_COUNTS = {
    "urlsV2": 3,
    "urls_blocked_retained": 1,
    "custom_domains": 1,
    "tags": 2,
    # url_id-chunk pass (4, pre-claim clicks included) + owner mop-up (2).
    "clicks": 6,
    "api_keys": 1,
    "verification_tokens": 4,
    "page_layouts": 1,
    "app_grants": 2,
    "webhook_endpoints": 1,
    "webhook_events": 5,
    "webhook_deliveries": 6,
    "report_submissions": 1,
    "reports_pulled": 2,
    "feature_flags_pulled": 1,
    "subscriptions": 1,
    "entitlement_overrides": 2,
    "entitlement_events": 3,
    "r2_objects": 2,
}


# ── erase ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_erase_returns_counts_for_every_collection():
    stubs = Stubs()
    counts = await _service(stubs).erase(UID)
    assert counts == EXPECTED_COUNTS


@pytest.mark.asyncio
async def test_erase_follows_cascade_order_user_doc_last():
    stubs = Stubs()
    await _service(stubs).erase(UID)
    names = stubs.names()
    # Claim FIRST (a mid-sweep restore must win before anything is touched);
    # mail LAST, only after the doc is gone (the one non-idempotent step).
    assert names[0] == "users.claim"
    assert names[-3:] == ["posthog", "users.delete_hard", "mailer"]
    # Cascade order from the plan: claim → links → domains → clicks →
    # satellites → pulls → R2 → PostHog → users → email.
    milestones = [n for n in names if n not in ("r2.list", "r2.delete")]
    assert milestones == [
        "users.claim",
        "urls",
        "domains",
        "tags",
        # url_id-chunk deletes FIRST (pre-claim clicks), owner mop-up second.
        "clicks.by_url_ids",
        "clicks",
        "api_keys",
        "tokens",
        "page_layouts",
        "app_grants",
        "webhook_endpoints",
        "webhook_events",
        "webhook_deliveries",
        "report_submissions",
        "reports_pull",
        "feature_flags_pull",
        "subscriptions",
        "entitlement_overrides",
        "entitlement_events",
        "posthog",
        "users.delete_hard",
        "mailer",
    ]


@pytest.mark.asyncio
async def test_erase_failed_claim_returns_empty_without_side_effects():
    """Claim lost (restored mid-sweep, not due, or already purged) — the
    account is untouched: no delete ran, no mail went out."""
    stubs = Stubs()
    stubs.user_repo.claim_for_erasure = AsyncMock(return_value=False)
    counts = await _service(stubs).erase(UID)
    assert counts == {}
    assert stubs.calls == []


@pytest.mark.asyncio
async def test_erase_missing_user_after_claim_returns_empty():
    stubs = Stubs()
    stubs.user_repo.find_by_id = AsyncMock(return_value=None)
    counts = await _service(stubs).erase(UID)
    assert counts == {}
    # Only the claim ran — no deletes, no mail.
    assert stubs.names() == ["users.claim"]


@pytest.mark.asyncio
async def test_erase_uses_the_right_predicates():
    stubs = Stubs()
    await _service(stubs).erase(UID)
    stubs.click_repo.delete_by_url_ids.assert_awaited_once_with(URL_IDS)
    stubs.click_repo.delete_by_owner.assert_awaited_once_with(UID)
    stubs.token_repo.delete_by_user_or_email.assert_awaited_once_with(UID, EMAIL)
    stubs.report_submission_repo.delete_by_reporter.assert_awaited_once_with(UID, EMAIL)
    stubs.feature_flag_repo.pull_allowlisted.assert_awaited_once_with(UID, EMAIL)
    stubs.report_repo.pull_reporter.assert_awaited_once_with(UID)
    stubs.posthog.delete_person.assert_awaited_once_with(str(UID))
    # Email captured from the doc BEFORE any deletion, then used for the
    # final confirmation.
    stubs.mailer.send_erasure_confirmation.assert_awaited_once_with(EMAIL)
    stubs.user_repo.delete_hard.assert_awaited_once_with(UID)


@pytest.mark.asyncio
async def test_erase_crash_midway_keeps_user_doc():
    stubs = Stubs()
    stubs.click_repo.delete_by_owner = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await _service(stubs).erase(UID)
    stubs.user_repo.delete_hard.assert_not_awaited()
    stubs.mailer.send_erasure_confirmation.assert_not_awaited()


@pytest.mark.asyncio
async def test_erase_repo_failure_propagates():
    stubs = Stubs()
    stubs.webhook_delivery_repo.delete_by_user = AsyncMock(
        side_effect=RuntimeError("mongo down")
    )
    with pytest.raises(RuntimeError):
        await _service(stubs).erase(UID)
    stubs.user_repo.delete_hard.assert_not_awaited()


@pytest.mark.asyncio
async def test_erase_posthog_failure_is_swallowed():
    stubs = Stubs()
    stubs.posthog.delete_person = AsyncMock(side_effect=RuntimeError("api down"))
    counts = await _service(stubs).erase(UID)
    assert counts == EXPECTED_COUNTS
    stubs.user_repo.delete_hard.assert_awaited_once_with(UID)


@pytest.mark.asyncio
async def test_erase_mailer_failure_is_swallowed():
    """A mail outage must not park an account in PENDING_DELETION forever."""
    stubs = Stubs()
    stubs.mailer.send_erasure_confirmation = AsyncMock(
        side_effect=RuntimeError("smtp down")
    )
    counts = await _service(stubs).erase(UID)
    assert counts == EXPECTED_COUNTS
    stubs.user_repo.delete_hard.assert_awaited_once_with(UID)


@pytest.mark.asyncio
async def test_erase_skips_confirmation_mail_when_doc_already_gone():
    """delete_hard finding nothing means the claim raced away — the
    "permanently deleted" mail must not go out for a doc we didn't delete."""
    stubs = Stubs()
    stubs.user_repo.delete_hard = AsyncMock(return_value=False)
    counts = await _service(stubs).erase(UID)
    assert counts == EXPECTED_COUNTS
    stubs.mailer.send_erasure_confirmation.assert_not_awaited()


@pytest.mark.asyncio
async def test_erase_noop_externals_by_default():
    stubs = Stubs()
    svc = _service(stubs, posthog=None, mailer=None)
    counts = await svc.erase(UID)
    assert counts == EXPECTED_COUNTS
    assert isinstance(svc._posthog, NoopPostHogEraser)
    assert isinstance(svc._mailer, NoopErasureMailer)


@pytest.mark.asyncio
async def test_erase_chunks_click_deletes_by_url_id():
    """A six-figure link set must never push one giant $in at the
    time-series collection — 1001 ids ⇒ ceil(1001/500) = 3 chunk calls."""
    stubs = Stubs()
    many_ids = [ObjectId() for _ in range(1001)]
    stubs.url_service.delete_all_by_owner = AsyncMock(
        return_value=OwnerUrlErasure(deleted=1001, blocked_retained=0, url_ids=many_ids)
    )
    stubs.click_repo.delete_by_url_ids = AsyncMock(return_value=10)
    counts = await _service(stubs).erase(UID)
    chunks = [c.args[0] for c in stubs.click_repo.delete_by_url_ids.await_args_list]
    assert [len(c) for c in chunks] == [500, 500, 1]
    assert [i for chunk in chunks for i in chunk] == many_ids
    # 3 chunk deletes (10 each) + owner mop-up (2 from the stub).
    assert counts["clicks"] == 32


@pytest.mark.asyncio
async def test_erase_with_no_links_skips_url_id_click_pass():
    stubs = Stubs()
    stubs.url_service.delete_all_by_owner = AsyncMock(
        return_value=OwnerUrlErasure(deleted=0, blocked_retained=0, url_ids=[])
    )
    counts = await _service(stubs).erase(UID)
    stubs.click_repo.delete_by_url_ids.assert_not_awaited()
    # Owner-based mop-up still runs — clicks on long-deleted links.
    stubs.click_repo.delete_by_owner.assert_awaited_once_with(UID)
    assert counts["clicks"] == 2


# ── erase: R2 sweep ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_erase_sweeps_r2_under_both_owner_prefixes():
    stubs = Stubs()
    counts = await _service(stubs).erase(UID)
    prefix = owner_key_prefix(UID, SECRET)
    listed = [args[0] for name, args in stubs.calls if name == "r2.list"]
    assert listed == [f"profile-pictures/{prefix}/", f"og/{prefix}/"]
    deleted = [args[0] for name, args in stubs.calls if name == "r2.delete"]
    assert deleted == [
        f"profile-pictures/{prefix}/a.png",
        f"og/{prefix}/b.png",
    ]
    assert counts["r2_objects"] == 2


@pytest.mark.asyncio
async def test_erase_without_r2_counts_zero():
    stubs = Stubs()
    counts = await _service(stubs, r2=None).erase(UID)
    assert counts["r2_objects"] == 0
    stubs.user_repo.delete_hard.assert_awaited_once_with(UID)


@pytest.mark.asyncio
async def test_erase_unconfigured_r2_counts_zero():
    stubs = Stubs()
    stubs.r2_storage.is_configured = False
    counts = await _service(stubs).erase(UID)
    assert counts["r2_objects"] == 0
    stubs.r2_storage.list_keys.assert_not_awaited()


@pytest.mark.asyncio
async def test_erase_prefers_the_pinned_storage_prefix():
    """The prefix pinned at upload time is where the objects actually live;
    when it matches the computed HMAC there is exactly one sweep."""
    stubs = Stubs()
    pinned = owner_key_prefix(UID, SECRET)
    stubs.user_repo.find_by_id = AsyncMock(
        return_value=_user_doc(storage_prefix=pinned)
    )
    counts = await _service(stubs).erase(UID)
    listed = [args[0] for name, args in stubs.calls if name == "r2.list"]
    assert listed == [f"profile-pictures/{pinned}/", f"og/{pinned}/"]
    assert counts["r2_objects"] == 2


@pytest.mark.asyncio
async def test_erase_sweeps_both_prefixes_after_secret_rotation():
    """Stored and computed prefixes diverge after a SECRET_KEY rotation —
    uploads may sit under either, so BOTH get swept (stored first)."""
    stubs = Stubs()
    stubs.user_repo.find_by_id = AsyncMock(
        return_value=_user_doc(storage_prefix="0ld5ecretpref1x0")
    )
    computed = owner_key_prefix(UID, SECRET)
    await _service(stubs).erase(UID)
    listed = [args[0] for name, args in stubs.calls if name == "r2.list"]
    assert listed == [
        "profile-pictures/0ld5ecretpref1x0/",
        "og/0ld5ecretpref1x0/",
        f"profile-pictures/{computed}/",
        f"og/{computed}/",
    ]


@pytest.mark.asyncio
async def test_erase_r2_delete_failure_keeps_user_doc():
    """A failed object delete means the sweep is incomplete — raise so the
    next sweep retries instead of orphaning the object forever."""
    stubs = Stubs()
    stubs.r2_storage.delete_object = AsyncMock(return_value=False)
    with pytest.raises(R2StorageError):
        await _service(stubs).erase(UID)
    stubs.user_repo.delete_hard.assert_not_awaited()


# ── sweep ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sweep_nothing_due():
    stubs = Stubs()
    result = await _service(stubs).sweep()
    assert result == {"erased": 0, "failed": 0, "skipped": 0, "deferred": 0}
    stubs.user_repo.delete_hard.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_respects_batch_limit():
    stubs = Stubs()
    await _service(stubs, batch_limit=7).sweep()
    assert stubs.user_repo.find_purge_due.await_args.kwargs["limit"] == 7


@pytest.mark.asyncio
async def test_claim_lease_threaded_into_claim_and_sweep_view():
    """The configured lease reaches both repo calls — the claim's guard
    and the sweep's due-view must judge staleness by the same clock."""
    stubs = Stubs()
    stubs.user_repo.find_purge_due = AsyncMock(return_value=[_user_doc()])
    await _service(stubs, claim_lease_seconds=1234).sweep()
    assert stubs.user_repo.find_purge_due.await_args.kwargs["lease_seconds"] == 1234
    assert stubs.user_repo.claim_for_erasure.await_args.kwargs["lease_seconds"] == 1234


@pytest.mark.asyncio
async def test_sweep_isolates_per_user_failures():
    stubs = Stubs()
    user_a, user_b = _user_doc(UID), _user_doc(UID2, email="b@example.com")
    stubs.user_repo.find_purge_due = AsyncMock(return_value=[user_a, user_b])
    stubs.user_repo.find_by_id = AsyncMock(
        side_effect=lambda uid: user_a if uid == UID else user_b
    )
    # First user's cascade blows up; the second must still be erased.
    stubs.click_repo.delete_by_owner = AsyncMock(side_effect=[RuntimeError("boom"), 2])
    result = await _service(stubs).sweep()
    assert result == {"erased": 1, "failed": 1, "skipped": 0, "deferred": 0}
    stubs.user_repo.delete_hard.assert_awaited_once_with(UID2)


@pytest.mark.asyncio
async def test_sweep_counts_all_successes():
    stubs = Stubs()
    user_a, user_b = _user_doc(UID), _user_doc(UID2, email="b@example.com")
    stubs.user_repo.find_purge_due = AsyncMock(return_value=[user_a, user_b])
    stubs.user_repo.find_by_id = AsyncMock(
        side_effect=lambda uid: user_a if uid == UID else user_b
    )
    result = await _service(stubs).sweep()
    assert result == {"erased": 2, "failed": 0, "skipped": 0, "deferred": 0}
    assert stubs.user_repo.delete_hard.await_count == 2


@pytest.mark.asyncio
async def test_sweep_mid_sweep_restore_leaves_account_intact():
    """THE race: selected for the batch, then restored before its turn —
    the claim fails, nothing is deleted, and the sweep reports a skip."""
    stubs = Stubs()
    user_a, user_b = _user_doc(UID), _user_doc(UID2, email="b@example.com")
    stubs.user_repo.find_purge_due = AsyncMock(return_value=[user_a, user_b])
    stubs.user_repo.find_by_id = AsyncMock(
        side_effect=lambda uid: user_a if uid == UID else user_b
    )
    # user_a restored mid-sweep: its claim fails; user_b still erases.
    stubs.user_repo.claim_for_erasure = AsyncMock(
        side_effect=lambda uid, now, lease_seconds: uid != UID
    )
    result = await _service(stubs).sweep()
    assert result == {"erased": 1, "failed": 0, "skipped": 1, "deferred": 0}
    stubs.user_repo.delete_hard.assert_awaited_once_with(UID2)
    stubs.mailer.send_erasure_confirmation.assert_awaited_once_with("b@example.com")


@pytest.mark.asyncio
async def test_sweep_defers_when_time_budget_exhausted():
    """Budget spent before the batch drains — no NEW erasures start, the
    remainder defers to the next sweep, and the count is reported."""
    stubs = Stubs()
    user_a, user_b = _user_doc(UID), _user_doc(UID2, email="b@example.com")
    stubs.user_repo.find_purge_due = AsyncMock(return_value=[user_a, user_b])
    result = await _service(stubs, time_budget_seconds=0).sweep()
    assert result == {"erased": 0, "failed": 0, "skipped": 0, "deferred": 2}
    stubs.user_repo.claim_for_erasure.assert_not_awaited()
    stubs.user_repo.delete_hard.assert_not_awaited()


# ── scheduler registration ───────────────────────────────────────────────────


class TestErasureSweepTask:
    def test_task_shape(self):
        service = _service(Stubs())
        task = erasure_sweep_task(service)
        assert task.name == "account-erasure-sweep"
        assert task.schedule == "*/10 * * * *"
        assert task.fn == service.sweep

    def test_registered_in_composed_registry(self):
        """Mirror of the exact composition wiring and the worker perform."""
        task = erasure_sweep_task(_service(Stubs()))
        registry = build_task_registry([task])
        registered = registry.get(ERASURE_SWEEP_TASK)
        assert registered is task
        assert registered.schedule == "*/10 * * * *"
