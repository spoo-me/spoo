"""Unit tests for CustomDomainService."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from config import CustomDomainSettings
from errors import (
    AppError,
    DomainAlreadyRegisteredError,
    DomainBlocklistedError,
    DomainNotVerifiedError,
    DomainQuotaExceededError,
    FeatureDisabledError,
    ForbiddenError,
    InvalidDomainTransitionError,
    LimitReachedError,
    NotFoundError,
)
from schemas.dto.requests.custom_domain import (
    CreateCustomDomainRequest,
    ListCustomDomainsQuery,
    UpdateCustomDomainRequest,
)
from schemas.enums.domain_status import DomainStatus, VerificationMethod
from schemas.models.custom_domain import CustomDomainDoc
from services.custom_domain_service import CustomDomainService
from services.registrar.protocol import RegistrationResult
from services.verifiers.protocol import VerificationResult

USER_OID = ObjectId("aaaaaaaaaaaaaaaaaaaaaaaa")
DOMAIN_OID = ObjectId("bbbbbbbbbbbbbbbbbbbbbbbb")


def _user(user_id=USER_OID):
    u = MagicMock()
    u.user_id = user_id
    u.email_verified = True
    return u


def _doc(
    fqdn="links.acme.com",
    status=DomainStatus.PENDING,
    method=VerificationMethod.CNAME,
    owner_id=USER_OID,
    domain_id=DOMAIN_OID,
):
    return CustomDomainDoc(
        id=domain_id,
        fqdn=fqdn,
        owner_id=owner_id,
        status=status,
        verification_method=method,
        verification_token="token-abc",
        created_at=datetime.now(timezone.utc),
    )


def _settings(**overrides):
    base = {"enabled": True}
    base.update(overrides)
    return CustomDomainSettings(**base)


def _build_service(
    *,
    enabled=True,
    repo=None,
    verifiers=None,
    edge=None,
    registrar=None,
    tenant_resolver=None,
    blocked_domain_repo=None,
    redis=None,
    extra_settings=None,
    url_service=None,
    preflight_cname_target=None,
):
    repo = repo or AsyncMock()
    repo.insert = AsyncMock(return_value=DOMAIN_OID)
    repo.delete_by_id = AsyncMock(return_value=True)
    repo.update_edge_metadata = AsyncMock(return_value=True)
    repo.count_by_owner = AsyncMock(return_value=0)
    repo.find_by_fqdn = AsyncMock(return_value=None)
    repo.find_blocking_by_fqdn = AsyncMock(return_value=None)
    repo.find_by_id = AsyncMock(return_value=None)
    repo.update_status = AsyncMock(return_value=True)
    repo.set_eviction_pending = AsyncMock(return_value=True)

    if blocked_domain_repo is None:
        blocked_domain_repo = AsyncMock()
        # Default: nothing blocked.
        blocked_domain_repo.is_blocked = AsyncMock(return_value=False)

    if verifiers is None:
        cname = AsyncMock()
        cname.verify = AsyncMock(return_value=VerificationResult(True))
        a = AsyncMock()
        a.verify = AsyncMock(return_value=VerificationResult(True))
        txt = AsyncMock()
        txt.verify = AsyncMock(return_value=VerificationResult(True))
        cf = AsyncMock()
        cf.verify = AsyncMock(return_value=VerificationResult(True))
        verifiers = {
            VerificationMethod.CNAME: cname,
            VerificationMethod.A_RECORD: a,
            VerificationMethod.TXT_CHALLENGE: txt,
            VerificationMethod.CF_HTTP_DCV: cf,
            VerificationMethod.CF_DELEGATED_DCV: cf,
        }

    edge = edge or AsyncMock()
    # Default to "edge acked the eviction" so existing tests don't have to opt in.
    edge.announce_revoked = AsyncMock(return_value=True)

    if registrar is None:
        registrar = AsyncMock()
        # Default to backend_id=None so tests not exercising the metadata
        # writeback path pass through unchanged.
        registrar.register = AsyncMock(return_value=RegistrationResult(backend_id=None))

    tenant_resolver = tenant_resolver or AsyncMock()
    settings_kwargs = {"enabled": enabled, **(extra_settings or {})}
    settings = _settings(**settings_kwargs)
    return (
        CustomDomainService(
            repo=repo,
            verifiers=verifiers,
            edge_provisioner=edge,
            registrar=registrar,
            settings=settings,
            tenant_resolver=tenant_resolver,
            blocked_domain_repo=blocked_domain_repo,
            redis_client=redis,
            url_service=url_service,
            preflight_cname_target=preflight_cname_target,
        ),
        repo,
        verifiers,
        edge,
        tenant_resolver,
    )


class TestEnabledGate:
    @pytest.mark.asyncio
    async def test_create_refused_when_disabled(self):
        svc, _, _, _, _ = _build_service(enabled=False)
        req = CreateCustomDomainRequest(fqdn="links.acme.com")
        with pytest.raises(FeatureDisabledError):
            await svc.create(req, _user(), max_domains=5)

    @pytest.mark.asyncio
    async def test_verify_refused_when_disabled(self):
        svc, _, _, _, _ = _build_service(enabled=False)
        with pytest.raises(FeatureDisabledError):
            await svc.verify(DOMAIN_OID, _user())

    @pytest.mark.asyncio
    async def test_delete_refused_when_disabled(self):
        svc, _, _, _, _ = _build_service(enabled=False)
        with pytest.raises(FeatureDisabledError):
            await svc.delete(DOMAIN_OID, _user())

    @pytest.mark.asyncio
    async def test_list_allowed_when_disabled_for_rollback_visibility(self):
        # Reads stay open even when the master switch is off so existing
        # owners don't lose visibility into their domains during rollback.
        svc, repo, _, _, _ = _build_service(enabled=False)
        repo.list_by_owner = AsyncMock(return_value=[])
        repo.count_by_owner = AsyncMock(return_value=0)
        items, total = await svc.list_by_owner(_user(), ListCustomDomainsQuery())
        assert items == []
        assert total == 0


class TestCreate:
    @pytest.mark.asyncio
    async def test_creates_pending_doc_with_token(self):
        svc, repo, _, _, _ = _build_service()
        repo.find_by_id = AsyncMock(return_value=_doc(status=DomainStatus.PENDING))
        req = CreateCustomDomainRequest(fqdn="links.acme.com")
        doc = await svc.create(req, _user(), max_domains=5)
        assert doc.status == DomainStatus.PENDING
        repo.insert.assert_awaited_once()
        # Token always stamped — lets users switch backends later without re-registering.
        inserted = repo.insert.call_args.args[0]
        assert inserted["verification_token"] is not None

    @pytest.mark.asyncio
    async def test_picker_chooses_cf_when_backend_wired(self):
        svc, repo, _, _, _ = _build_service()
        repo.find_by_id = AsyncMock(return_value=_doc(status=DomainStatus.PENDING))
        await svc.create(
            CreateCustomDomainRequest(fqdn="links.acme.com"), _user(), max_domains=5
        )
        inserted = repo.insert.call_args.args[0]
        assert inserted["verification_method"] == VerificationMethod.CF_HTTP_DCV

    @pytest.mark.asyncio
    async def test_picker_falls_back_to_cname_when_only_le_wired(self):
        cname = AsyncMock()
        cname.verify = AsyncMock(return_value=VerificationResult(True))
        verifiers = {VerificationMethod.CNAME: cname}
        svc, repo, _, _, _ = _build_service(verifiers=verifiers)
        repo.find_by_id = AsyncMock(return_value=_doc(status=DomainStatus.PENDING))
        await svc.create(
            CreateCustomDomainRequest(fqdn="links.acme.com"), _user(), max_domains=5
        )
        inserted = repo.insert.call_args.args[0]
        assert inserted["verification_method"] == VerificationMethod.CNAME

    @pytest.mark.asyncio
    async def test_picker_chooses_a_record_for_apex_on_le_path(self):
        cname = AsyncMock()
        cname.verify = AsyncMock(return_value=VerificationResult(True))
        a = AsyncMock()
        a.verify = AsyncMock(return_value=VerificationResult(True))
        verifiers = {
            VerificationMethod.CNAME: cname,
            VerificationMethod.A_RECORD: a,
        }
        svc, repo, _, _, _ = _build_service(verifiers=verifiers)
        repo.find_by_id = AsyncMock(return_value=_doc(status=DomainStatus.PENDING))
        await svc.create(
            CreateCustomDomainRequest(fqdn="acme.com"), _user(), max_domains=5
        )
        inserted = repo.insert.call_args.args[0]
        assert inserted["verification_method"] == VerificationMethod.A_RECORD

    @pytest.mark.asyncio
    async def test_uniqueness_conflict_raises(self):
        svc, repo, _, _, _ = _build_service()
        repo.find_blocking_by_fqdn = AsyncMock(return_value=_doc())
        req = CreateCustomDomainRequest(fqdn="links.acme.com")
        with pytest.raises(
            DomainAlreadyRegisteredError,
            match="registered to another account",
        ):
            await svc.create(req, _user(), max_domains=5)

    @pytest.mark.asyncio
    async def test_uniqueness_ignores_revoked_docs(self):
        # REVOKED doc does NOT block re-registration of the same fqdn —
        # different owner can claim it (DCV gates takeover), or the same
        # owner can re-register after Remove. find_blocking_by_fqdn must
        # return None when the only matching doc is REVOKED.
        svc, repo, _, _, _ = _build_service()
        # Repo's find_blocking_by_fqdn is the gate; default mock returns
        # None to simulate "no blocking doc found" (REVOKED docs would be
        # filtered out at the query level).
        repo.find_blocking_by_fqdn = AsyncMock(return_value=None)
        repo.find_by_id = AsyncMock(return_value=_doc(status=DomainStatus.PENDING))
        req = CreateCustomDomainRequest(fqdn="links.acme.com")
        await svc.create(req, _user(), max_domains=5)  # must not raise
        repo.insert.assert_awaited()

    @pytest.mark.asyncio
    async def test_per_user_quota_enforced(self):
        svc, repo, _, _, _ = _build_service()
        repo.count_by_owner = AsyncMock(return_value=2)
        req = CreateCustomDomainRequest(fqdn="links.acme.com")
        with pytest.raises(LimitReachedError) as exc:
            await svc.create(req, _user(), max_domains=2)
        assert exc.value.to_dict()["limit"] == "custom_domains_max"
        assert exc.value.to_dict()["current"] == 2

    @pytest.mark.asyncio
    async def test_unlimited_plan_never_counts(self):
        svc, repo, _, _, _ = _build_service()
        repo.find_by_id = AsyncMock(return_value=_doc(status=DomainStatus.PENDING))
        repo.count_by_owner = AsyncMock(return_value=900)
        req = CreateCustomDomainRequest(fqdn="links.acme.com")
        await svc.create(req, _user(), max_domains=-1)
        repo.count_by_owner.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_blocklist_enforced_via_repo(self):
        blocked = AsyncMock()
        blocked.is_blocked = AsyncMock(return_value=True)
        svc, _, _, _, _ = _build_service(blocked_domain_repo=blocked)
        req = CreateCustomDomainRequest(fqdn="evil.com")
        with pytest.raises(DomainBlocklistedError):
            await svc.create(req, _user(), max_domains=5)
        blocked.is_blocked.assert_awaited_once_with("evil.com")

    @pytest.mark.asyncio
    async def test_blocklist_skipped_when_repo_missing(self):
        # Service must tolerate a None blocked_domain_repo (unit tests
        # that don't wire it) without crashing.
        svc, repo, _, _, _ = _build_service(blocked_domain_repo=None)
        # Wiring sets it; bypass for the test.
        svc._blocked_repo = None
        repo.find_by_id = AsyncMock(return_value=_doc(status=DomainStatus.PENDING))
        req = CreateCustomDomainRequest(fqdn="anything.example.com")
        # Must not raise — just skip the check.
        await svc.create(req, _user(), max_domains=5)

    @pytest.mark.asyncio
    async def test_duplicate_key_during_race_translates_to_friendly_error(self):
        # Precheck passes (no existing doc) but the unique-index backstop
        # catches a concurrent insert. Service must translate to the same
        # 409-friendly error instead of leaking a 500 with raw Mongo text.
        svc, repo, _, _, _ = _build_service()
        repo.find_by_fqdn = AsyncMock(return_value=None)
        repo.insert = AsyncMock(
            side_effect=DuplicateKeyError("E11000 duplicate key error: fqdn")
        )
        req = CreateCustomDomainRequest(fqdn="links.acme.com")
        with pytest.raises(DomainAlreadyRegisteredError):
            await svc.create(req, _user(), max_domains=5)


class TestVerify:
    @pytest.mark.asyncio
    async def test_success_transitions_to_active(self):
        svc, repo, _, _, _ = _build_service()
        starting = _doc(status=DomainStatus.PENDING)
        # find_by_id called twice: load_owned + refresh
        repo.find_by_id = AsyncMock(
            side_effect=[starting, _doc(status=DomainStatus.ACTIVE)]
        )
        await svc.verify(DOMAIN_OID, _user())
        # update_status called with ACTIVE + bump_last_verified_at=True
        repo.update_status.assert_awaited()
        args, kwargs = repo.update_status.call_args
        assert args[1] == DomainStatus.ACTIVE
        assert kwargs["bump_last_verified_at"] is True
        assert kwargs["last_verification_error"] is None

    @pytest.mark.asyncio
    async def test_failure_records_reason_keeps_status(self):
        svc, repo, verifiers, _, _ = _build_service()
        verifiers[VerificationMethod.CNAME].verify = AsyncMock(
            return_value=VerificationResult(False, reason="DNS NXDOMAIN")
        )
        starting = _doc(status=DomainStatus.PENDING)
        repo.find_by_id = AsyncMock(side_effect=[starting, starting])
        await svc.verify(DOMAIN_OID, _user())
        args, kwargs = repo.update_status.call_args
        # Stays in PENDING — never auto-suspends on a single user-driven verify.
        assert args[1] == DomainStatus.PENDING
        assert kwargs["last_verification_error"] == "DNS NXDOMAIN"

    @pytest.mark.asyncio
    async def test_verify_on_already_active_is_idempotent(self):
        # Idempotency: re-clicking "Verify" on an ACTIVE domain (e.g. after
        # a transient browser network blip) must not raise. ACTIVE→ACTIVE
        # is deliberately omitted from LEGAL_TRANSITIONS — _transition()
        # short-circuits self-loops to a plain update_status so the call
        # still bumps last_verified_at without going through the legality
        # check.
        svc, repo, _, _, _ = _build_service()
        active = _doc(status=DomainStatus.ACTIVE)
        repo.find_by_id = AsyncMock(side_effect=[active, active])
        await svc.verify(DOMAIN_OID, _user())
        args, kwargs = repo.update_status.call_args
        assert args[1] == DomainStatus.ACTIVE
        assert kwargs["bump_last_verified_at"] is True

    @pytest.mark.asyncio
    async def test_not_owner_forbidden(self):
        svc, repo, _, _, _ = _build_service()
        someone_else = ObjectId()
        repo.find_by_id = AsyncMock(return_value=_doc(owner_id=someone_else))
        with pytest.raises(ForbiddenError):
            await svc.verify(DOMAIN_OID, _user())

    @pytest.mark.asyncio
    async def test_not_found_raises(self):
        svc, repo, _, _, _ = _build_service()
        repo.find_by_id = AsyncMock(return_value=None)
        with pytest.raises(NotFoundError):
            await svc.verify(DOMAIN_OID, _user())

    @pytest.mark.asyncio
    async def test_preflight_failure_records_reason_and_skips_verifier(self):
        # When preflight fails, CF/verifier MUST NOT be invoked — otherwise CF
        # enters its 15-min backoff for unpropagated domains. The reason is
        # surfaced as last_verification_error on the doc; status unchanged.
        from services import dns_preflight as preflight_module

        svc, repo, verifiers, _, _ = _build_service(
            preflight_cname_target="customers.spoo.me"
        )
        verifier = verifiers[VerificationMethod.CNAME]
        starting = _doc(status=DomainStatus.PENDING)
        repo.find_by_id = AsyncMock(side_effect=[starting, starting])

        async def _bad_preflight(fqdn, target):
            return preflight_module.PreflightResult(
                ok=False, reason="DNS isn't reaching us yet"
            )

        with patch(
            "services.custom_domain_service.check_cname",
            side_effect=_bad_preflight,
        ):
            await svc.verify(DOMAIN_OID, _user())

        verifier.verify.assert_not_called()
        args, kwargs = repo.update_status.call_args
        assert args[1] == DomainStatus.PENDING
        assert kwargs["last_verification_error"] == "DNS isn't reaching us yet"

    @pytest.mark.asyncio
    async def test_preflight_success_lets_verifier_run(self):
        from services import dns_preflight as preflight_module

        svc, repo, verifiers, _, _ = _build_service(
            preflight_cname_target="customers.spoo.me"
        )
        verifier = verifiers[VerificationMethod.CNAME]
        starting = _doc(status=DomainStatus.PENDING)
        repo.find_by_id = AsyncMock(
            side_effect=[starting, _doc(status=DomainStatus.ACTIVE)]
        )

        async def _ok(fqdn, target):
            return preflight_module.PreflightResult(ok=True)

        with patch("services.custom_domain_service.check_cname", side_effect=_ok):
            await svc.verify(DOMAIN_OID, _user())

        verifier.verify.assert_awaited_once()


class TestDelete:
    @pytest.mark.asyncio
    async def test_revokes_and_announces(self):
        svc, repo, _, edge, _ = _build_service()
        repo.find_by_id = AsyncMock(return_value=_doc(status=DomainStatus.ACTIVE))
        await svc.delete(DOMAIN_OID, _user())
        # update_status to REVOKED + edge.announce_revoked called
        args, _ = repo.update_status.call_args
        assert args[1] == DomainStatus.REVOKED
        edge.announce_revoked.assert_awaited_once_with("links.acme.com")

    @pytest.mark.asyncio
    async def test_delete_on_already_revoked_is_idempotent(self):
        # Idempotency: retrying delete() on a REVOKED domain must not raise.
        # LEGAL_TRANSITIONS[REVOKED] is empty (terminal), so without the
        # self-loop short-circuit in _transition() this would raise
        # InvalidDomainTransitionError on every retry — bad UX for a user
        # who refreshes the dashboard or hits a transient network error
        # mid-delete.
        svc, repo, _, edge, _ = _build_service()
        repo.find_by_id = AsyncMock(return_value=_doc(status=DomainStatus.REVOKED))
        await svc.delete(DOMAIN_OID, _user())
        # update_status still called (records timestamps); status unchanged.
        args, _ = repo.update_status.call_args
        assert args[1] == DomainStatus.REVOKED
        edge.announce_revoked.assert_awaited_once()


class TestSuspend:
    @pytest.mark.asyncio
    async def test_active_to_suspended_legal(self):
        svc, repo, _, edge, _ = _build_service()
        repo.find_by_id = AsyncMock(return_value=_doc(status=DomainStatus.ACTIVE))
        await svc.suspend(DOMAIN_OID, reason="3 consecutive fails")
        args, _ = repo.update_status.call_args
        assert args[1] == DomainStatus.SUSPENDED
        edge.announce_revoked.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pending_to_suspended_illegal(self):
        # PENDING → SUSPENDED isn't in LEGAL_TRANSITIONS; should raise.
        svc, repo, _, _, _ = _build_service()
        repo.find_by_id = AsyncMock(return_value=_doc(status=DomainStatus.PENDING))
        with pytest.raises(InvalidDomainTransitionError):
            await svc.suspend(DOMAIN_OID, reason="test")


class TestCacheInvalidation:
    """Every state transition must invalidate the tenant resolver cache so
    the new state is visible to the next request, not after the TTL window.
    """

    @pytest.mark.asyncio
    async def test_verify_success_invalidates_negative_cache(self):
        svc, repo, _, _, resolver = _build_service()
        repo.find_by_id = AsyncMock(
            side_effect=[
                _doc(status=DomainStatus.PENDING),
                _doc(status=DomainStatus.ACTIVE),
            ]
        )
        await svc.verify(DOMAIN_OID, _user())
        resolver.invalidate.assert_awaited_once_with("links.acme.com")

    @pytest.mark.asyncio
    async def test_verify_failure_does_not_invalidate(self):
        svc, repo, verifiers, _, resolver = _build_service()
        verifiers[VerificationMethod.CNAME].verify = AsyncMock(
            return_value=VerificationResult(False, reason="DNS NXDOMAIN")
        )
        starting = _doc(status=DomainStatus.PENDING)
        repo.find_by_id = AsyncMock(side_effect=[starting, starting])
        await svc.verify(DOMAIN_OID, _user())
        # Status didn't change → no cache to drop.
        resolver.invalidate.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_invalidates_cache(self):
        svc, repo, _, _, resolver = _build_service()
        repo.find_by_id = AsyncMock(return_value=_doc(status=DomainStatus.ACTIVE))
        await svc.delete(DOMAIN_OID, _user())
        resolver.invalidate.assert_awaited_once_with("links.acme.com")

    @pytest.mark.asyncio
    async def test_suspend_invalidates_cache(self):
        svc, repo, _, _, resolver = _build_service()
        repo.find_by_id = AsyncMock(return_value=_doc(status=DomainStatus.ACTIVE))
        await svc.suspend(DOMAIN_OID, reason="test")
        resolver.invalidate.assert_awaited_once_with("links.acme.com")


class TestEvictionTracking:
    """Caddy revocation outcomes get persisted on the doc so the worker
    (PR5) can retry stale evictions. ``status`` and ``eviction_pending``
    are independent dimensions — both get updated, neither gates the other.
    """

    @pytest.mark.asyncio
    async def test_delete_with_caddy_ack_clears_eviction_pending(self):
        svc, repo, _, edge, _ = _build_service()
        edge.announce_revoked = AsyncMock(return_value=True)
        repo.find_by_id = AsyncMock(return_value=_doc(status=DomainStatus.ACTIVE))

        await svc.delete(DOMAIN_OID, _user())

        repo.set_eviction_pending.assert_awaited_once()
        kwargs = repo.set_eviction_pending.call_args.kwargs
        assert kwargs["pending"] is False
        assert kwargs["error"] is None

    @pytest.mark.asyncio
    async def test_delete_with_caddy_failure_marks_eviction_pending(self):
        svc, repo, _, edge, _ = _build_service()
        edge.announce_revoked = AsyncMock(return_value=False)
        repo.find_by_id = AsyncMock(return_value=_doc(status=DomainStatus.ACTIVE))

        await svc.delete(DOMAIN_OID, _user())

        kwargs = repo.set_eviction_pending.call_args.kwargs
        assert kwargs["pending"] is True
        assert "revoked" in kwargs["error"]

    @pytest.mark.asyncio
    async def test_suspend_with_caddy_failure_marks_eviction_pending(self):
        svc, repo, _, edge, _ = _build_service()
        edge.announce_revoked = AsyncMock(return_value=False)
        repo.find_by_id = AsyncMock(return_value=_doc(status=DomainStatus.ACTIVE))

        await svc.suspend(DOMAIN_OID, reason="test")

        kwargs = repo.set_eviction_pending.call_args.kwargs
        assert kwargs["pending"] is True
        assert "suspended" in kwargs["error"]

    @pytest.mark.asyncio
    async def test_status_transition_happens_regardless_of_caddy_outcome(self):
        # Even if Caddy fails, the status MUST move to REVOKED — the
        # in-app TenantResolver is the gating authority, not Caddy.
        svc, repo, _, edge, _ = _build_service()
        edge.announce_revoked = AsyncMock(return_value=False)
        repo.find_by_id = AsyncMock(return_value=_doc(status=DomainStatus.ACTIVE))

        await svc.delete(DOMAIN_OID, _user())

        # update_status was called with REVOKED first
        first_call = repo.update_status.call_args_list[0]
        assert first_call.args[1] == DomainStatus.REVOKED


class TestSuspendNotFound:
    @pytest.mark.asyncio
    async def test_suspend_missing_domain_is_noop(self):
        # Worker may race with a concurrent delete — domain disappears
        # between scan and suspend. Must silently noop, not crash.
        svc, repo, _, edge, _ = _build_service()
        repo.find_by_id = AsyncMock(return_value=None)

        # Must not raise
        await svc.suspend(DOMAIN_OID, reason="missing_domain_ok")

        repo.update_status.assert_not_called()
        edge.announce_revoked.assert_not_called()


class TestRemoveRevoked:
    @pytest.mark.asyncio
    async def test_hard_deletes_revoked_doc(self):
        svc, repo, _, _, _ = _build_service()
        repo.find_by_id = AsyncMock(return_value=_doc(status=DomainStatus.REVOKED))
        await svc.remove_revoked(DOMAIN_OID, _user())
        repo.delete_by_id.assert_awaited_once_with(DOMAIN_OID)

    @pytest.mark.asyncio
    async def test_refuses_active_doc(self):
        svc, repo, _, _, _ = _build_service()
        repo.find_by_id = AsyncMock(return_value=_doc(status=DomainStatus.ACTIVE))
        with pytest.raises(InvalidDomainTransitionError):
            await svc.remove_revoked(DOMAIN_OID, _user())
        repo.delete_by_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_refuses_pending_doc(self):
        svc, repo, _, _, _ = _build_service()
        repo.find_by_id = AsyncMock(return_value=_doc(status=DomainStatus.PENDING))
        with pytest.raises(InvalidDomainTransitionError):
            await svc.remove_revoked(DOMAIN_OID, _user())
        repo.delete_by_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_refuses_non_owner(self):
        # _load_owned raises ForbiddenError before status check fires.
        svc, repo, _, _, _ = _build_service()
        other = _doc(status=DomainStatus.REVOKED)
        other.owner_id = ObjectId()  # different from _user()'s id
        repo.find_by_id = AsyncMock(return_value=other)
        with pytest.raises(ForbiddenError):
            await svc.remove_revoked(DOMAIN_OID, _user())
        repo.delete_by_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_refuses_missing_doc(self):
        svc, repo, _, _, _ = _build_service()
        repo.find_by_id = AsyncMock(return_value=None)
        with pytest.raises(NotFoundError):
            await svc.remove_revoked(DOMAIN_OID, _user())
        repo.delete_by_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_raises_not_found_when_hard_delete_noops(self):
        svc, repo, _, _, _ = _build_service()
        repo.find_by_id = AsyncMock(return_value=_doc(status=DomainStatus.REVOKED))
        repo.delete_by_id = AsyncMock(return_value=False)
        with pytest.raises(NotFoundError):
            await svc.remove_revoked(DOMAIN_OID, _user())
        repo.delete_by_id.assert_awaited_once_with(DOMAIN_OID)

    @pytest.mark.asyncio
    async def test_respects_feature_flag(self):
        svc, repo, _, _, _ = _build_service(enabled=False)
        with pytest.raises(FeatureDisabledError):
            await svc.remove_revoked(DOMAIN_OID, _user())
        repo.find_by_id.assert_not_called()
        repo.delete_by_id.assert_not_called()


class TestVerifyAttemptsQuota:
    @pytest.mark.asyncio
    async def test_quota_increments_redis_counter(self):
        redis = AsyncMock()
        redis.incr = AsyncMock(return_value=1)
        redis.expire = AsyncMock()
        svc, repo, _, _, _ = _build_service(redis=redis)
        starting = _doc(status=DomainStatus.PENDING)
        repo.find_by_id = AsyncMock(side_effect=[starting, starting])

        await svc.verify(DOMAIN_OID, _user())

        redis.incr.assert_awaited_once()
        # First incr → expire is set so the counter rolls over after the window
        redis.expire.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_quota_exceeded_raises_quota_error(self):
        redis = AsyncMock()
        redis.incr = AsyncMock(return_value=99)  # well over default cap of 5
        redis.expire = AsyncMock()
        svc, repo, _, _, _ = _build_service(redis=redis)
        repo.find_by_id = AsyncMock(return_value=_doc(status=DomainStatus.PENDING))

        with pytest.raises(DomainQuotaExceededError):
            await svc.verify(DOMAIN_OID, _user())

    @pytest.mark.asyncio
    async def test_quota_fails_open_when_redis_errors(self):
        # If Redis is down we degrade to "no quota enforcement" rather than
        # blocking all verifies — staff allowlist + per-user-per-domain
        # natural rate-limit cover the abuse vector.
        redis = AsyncMock()
        redis.incr = AsyncMock(side_effect=Exception("redis down"))
        svc, repo, _, _, _ = _build_service(redis=redis)
        starting = _doc(status=DomainStatus.PENDING)
        repo.find_by_id = AsyncMock(side_effect=[starting, starting])

        # Must not raise — verify proceeds normally despite Redis fault.
        await svc.verify(DOMAIN_OID, _user())


class TestReverifyActive:
    @pytest.mark.asyncio
    async def test_success_bumps_last_verified_and_clears_error(self):
        svc, repo, _, _, _ = _build_service()
        d = _doc(status=DomainStatus.ACTIVE)
        repo.find_stale_active = AsyncMock(return_value=[d])

        result_pairs = await svc.reverify_active(batch_size=10)

        assert len(result_pairs) == 1
        ok_call = repo.update_status.call_args
        # ACTIVE remains, last_verified_at bumped, error cleared
        assert ok_call.args[1] == DomainStatus.ACTIVE
        assert ok_call.kwargs["bump_last_verified_at"] is True
        assert ok_call.kwargs["last_verification_error"] is None

    @pytest.mark.asyncio
    async def test_failure_records_reason_keeps_status(self):
        svc, repo, verifiers, _, _ = _build_service()
        verifiers[VerificationMethod.CNAME].verify = AsyncMock(
            return_value=VerificationResult(False, reason="DNS NXDOMAIN")
        )
        d = _doc(status=DomainStatus.ACTIVE)
        repo.find_stale_active = AsyncMock(return_value=[d])

        await svc.reverify_active(batch_size=10)

        # Status unchanged (worker doesn't auto-suspend on a single fail —
        # that's the consecutive-failure counter's job, lives in the worker)
        call = repo.update_status.call_args
        assert call.args[1] == DomainStatus.ACTIVE
        # bump_last_verified_at omitted on failure → default False applies.
        assert call.kwargs.get("bump_last_verified_at", False) is False
        assert call.kwargs["last_verification_error"] == "DNS NXDOMAIN"

    @pytest.mark.asyncio
    async def test_skips_doc_when_verifier_missing(self):
        # Defensive: if a doc references a verification_method that isn't
        # wired (legacy data), the loop must skip it without crashing.
        svc, repo, _, _, _ = _build_service(verifiers={})  # no verifiers
        d = _doc(status=DomainStatus.ACTIVE)
        repo.find_stale_active = AsyncMock(return_value=[d])

        result_pairs = await svc.reverify_active(batch_size=10)

        assert result_pairs == []
        repo.update_status.assert_not_called()


class TestRegistrarIntegration:
    @pytest.mark.asyncio
    async def test_create_persists_backend_id_and_metadata(self):
        registrar = AsyncMock()
        registrar.register = AsyncMock(
            return_value=RegistrationResult(
                backend_id="cf-1",
                backend_metadata={
                    "cf_status": "pending",
                    "cf_ssl_status": "initializing",
                },
            )
        )
        svc, repo, _, _, _ = _build_service(registrar=registrar)
        repo.find_by_id = AsyncMock(return_value=_doc(status=DomainStatus.PENDING))
        req = CreateCustomDomainRequest(fqdn="links.acme.com")

        await svc.create(req, _user(), max_domains=5)

        registrar.register.assert_awaited_once()
        repo.update_edge_metadata.assert_awaited_once()
        kwargs = repo.update_edge_metadata.call_args.kwargs
        assert kwargs["cf_hostname_id"] == "cf-1"
        assert kwargs["cf_status"] == "pending"
        assert kwargs["cf_ssl_status"] == "initializing"

    @pytest.mark.asyncio
    async def test_create_rolls_back_doc_on_registration_failure(self):
        registrar = AsyncMock()
        registrar.register = AsyncMock(side_effect=RuntimeError("CF down"))
        svc, repo, _, _, _ = _build_service(registrar=registrar)
        req = CreateCustomDomainRequest(fqdn="links.acme.com")

        with pytest.raises(RuntimeError):
            await svc.create(req, _user(), max_domains=5)

        # Doc inserted, then deleted because registration blew up.
        repo.insert.assert_awaited_once()
        repo.delete_by_id.assert_awaited_once_with(DOMAIN_OID)

    @pytest.mark.asyncio
    async def test_rollback_failure_logs_and_still_raises_original(self):
        # If delete_by_id itself fails after registrar fails, the original
        # registration error must still surface to the caller. Orphan doc is
        # logged loud so an operator can clean up via mongosh.
        registrar = AsyncMock()
        registrar.register = AsyncMock(side_effect=RuntimeError("CF down"))
        svc, repo, _, _, _ = _build_service(registrar=registrar)
        repo.delete_by_id = AsyncMock(side_effect=Exception("mongo down"))
        req = CreateCustomDomainRequest(fqdn="links.acme.com")

        with pytest.raises(RuntimeError, match="CF down"):
            await svc.create(req, _user(), max_domains=5)

        # Both attempted exactly once.
        repo.insert.assert_awaited_once()
        repo.delete_by_id.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_create_persists_dns_instructions_for_dashboard(self):
        registrar = AsyncMock()
        instructions = [
            {"type": "CNAME", "name": "links.acme.com", "value": "customers.spoo.me"},
            {
                "type": "CNAME",
                "name": "_acme-challenge.links.acme.com",
                "value": "links.acme.com.abc.dcv.cloudflare.com",
            },
        ]
        registrar.register = AsyncMock(
            return_value=RegistrationResult(
                backend_id="cf-1", instructions=instructions
            )
        )
        svc, repo, _, _, _ = _build_service(registrar=registrar)
        repo.find_by_id = AsyncMock(return_value=_doc(status=DomainStatus.PENDING))
        req = CreateCustomDomainRequest(fqdn="links.acme.com")

        await svc.create(req, _user(), max_domains=5)

        kwargs = repo.update_edge_metadata.call_args.kwargs
        assert kwargs["dns_instructions"] == instructions

    @pytest.mark.asyncio
    async def test_create_skips_metadata_update_when_registrar_returns_no_backend_id(
        self,
    ):
        # When a registrar returns backend_id=None (no backend metadata to
        # persist), the service must not bother calling update_edge_metadata.
        svc, repo, _, _, _ = _build_service()  # default registrar returns None
        repo.find_by_id = AsyncMock(return_value=_doc(status=DomainStatus.PENDING))
        req = CreateCustomDomainRequest(fqdn="links.acme.com")

        await svc.create(req, _user(), max_domains=5)

        repo.update_edge_metadata.assert_not_called()


class TestVerifierTokenSelection:
    @pytest.mark.asyncio
    async def test_cf_method_passes_cf_hostname_id_to_verifier(self):
        verifier = AsyncMock()
        verifier.verify = AsyncMock(return_value=VerificationResult(True))
        verifiers = {VerificationMethod.CF_DELEGATED_DCV: verifier}
        svc, repo, _, _, _ = _build_service(verifiers=verifiers)
        cf_doc = _doc(method=VerificationMethod.CF_DELEGATED_DCV)
        cf_doc.cf_hostname_id = "cf-xyz"
        repo.find_by_id = AsyncMock(side_effect=[cf_doc, cf_doc])

        await svc.verify(DOMAIN_OID, _user())

        verifier.verify.assert_awaited_once_with("links.acme.com", "cf-xyz")

    @pytest.mark.asyncio
    async def test_dns_method_passes_verification_token(self):
        svc, repo, verifiers, _, _ = _build_service()
        cname_verifier = verifiers[VerificationMethod.CNAME]
        starting = _doc(method=VerificationMethod.CNAME)
        repo.find_by_id = AsyncMock(side_effect=[starting, starting])

        await svc.verify(DOMAIN_OID, _user())

        cname_verifier.verify.assert_awaited_once_with("links.acme.com", "token-abc")


class TestGetOwnedById:
    @pytest.mark.asyncio
    async def test_returns_doc(self):
        svc, repo, _, _, _ = _build_service()
        repo.find_by_id = AsyncMock(return_value=_doc(status=DomainStatus.PENDING))
        result = await svc.get_owned_by_id(DOMAIN_OID, _user())
        assert result.id == DOMAIN_OID

    @pytest.mark.asyncio
    async def test_unknown_id_raises_404(self):
        svc, repo, _, _, _ = _build_service()
        repo.find_by_id = AsyncMock(return_value=None)
        with pytest.raises(NotFoundError):
            await svc.get_owned_by_id(DOMAIN_OID, _user())

    @pytest.mark.asyncio
    async def test_other_owner_raises_403(self):
        svc, repo, _, _, _ = _build_service()
        repo.find_by_id = AsyncMock(
            return_value=_doc(owner_id=ObjectId("ffffffffffffffffffffffff"))
        )
        with pytest.raises(ForbiddenError):
            await svc.get_owned_by_id(DOMAIN_OID, _user())


class TestAssertOwned:
    @pytest.mark.asyncio
    async def test_owned_returns_doc(self):
        svc, repo, _, _, _ = _build_service()
        repo.find_by_fqdn = AsyncMock(return_value=_doc(status=DomainStatus.ACTIVE))
        result = await svc.assert_owned(_user(), "links.acme.com")
        assert result.fqdn == "links.acme.com"

    @pytest.mark.asyncio
    async def test_unknown_domain_raises_404(self):
        svc, repo, _, _, _ = _build_service()
        repo.find_by_fqdn = AsyncMock(return_value=None)
        with pytest.raises(NotFoundError):
            await svc.assert_owned(_user(), "nope.example.com")

    @pytest.mark.asyncio
    async def test_other_owner_raises_403(self):
        svc, repo, _, _, _ = _build_service()
        repo.find_by_fqdn = AsyncMock(
            return_value=_doc(owner_id=ObjectId("ffffffffffffffffffffffff"))
        )
        with pytest.raises(ForbiddenError):
            await svc.assert_owned(_user(), "links.acme.com")

    @pytest.mark.asyncio
    async def test_owned_accepts_any_status(self):
        # Bulk-delete cleanup path needs to work on revoked/suspended domains too.
        svc, repo, _, _, _ = _build_service()
        repo.find_by_fqdn = AsyncMock(return_value=_doc(status=DomainStatus.SUSPENDED))
        doc = await svc.assert_owned(_user(), "links.acme.com")
        assert doc.status == DomainStatus.SUSPENDED


class TestAssertOwnedAndActive:
    @pytest.mark.asyncio
    async def test_active_returns_doc(self):
        svc, repo, _, _, _ = _build_service()
        repo.find_by_fqdn = AsyncMock(return_value=_doc(status=DomainStatus.ACTIVE))
        doc = await svc.assert_owned_and_active(_user(), "links.acme.com")
        assert doc.status == DomainStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_pending_raises_422(self):
        svc, repo, _, _, _ = _build_service()
        repo.find_by_fqdn = AsyncMock(return_value=_doc(status=DomainStatus.PENDING))
        with pytest.raises(DomainNotVerifiedError):
            await svc.assert_owned_and_active(_user(), "links.acme.com")

    @pytest.mark.asyncio
    async def test_suspended_raises_422(self):
        svc, repo, _, _, _ = _build_service()
        repo.find_by_fqdn = AsyncMock(return_value=_doc(status=DomainStatus.SUSPENDED))
        with pytest.raises(DomainNotVerifiedError):
            await svc.assert_owned_and_active(_user(), "links.acme.com")

    @pytest.mark.asyncio
    async def test_revoked_raises_422(self):
        svc, repo, _, _, _ = _build_service()
        repo.find_by_fqdn = AsyncMock(return_value=_doc(status=DomainStatus.REVOKED))
        with pytest.raises(DomainNotVerifiedError):
            await svc.assert_owned_and_active(_user(), "links.acme.com")


class TestDeleteCascade:
    @pytest.mark.asyncio
    async def test_delete_without_cascade_doesnt_touch_url_service(self):
        url_service = AsyncMock()
        url_service.delete_all_by_domain = AsyncMock(return_value=0)
        svc, repo, _, _, _ = _build_service(url_service=url_service)
        active = _doc(status=DomainStatus.ACTIVE)
        # First find_by_id loads the doc for ownership; second refreshes after.
        revoked = _doc(status=DomainStatus.REVOKED)
        repo.find_by_id = AsyncMock(side_effect=[active, revoked])

        doc, deleted = await svc.delete(DOMAIN_OID, _user())

        url_service.delete_all_by_domain.assert_not_called()
        assert deleted == 0
        assert doc.status == DomainStatus.REVOKED

    @pytest.mark.asyncio
    async def test_delete_with_cascade_calls_url_service(self):
        url_service = AsyncMock()
        url_service.delete_all_by_domain = AsyncMock(return_value=42)
        svc, repo, _, _, _ = _build_service(url_service=url_service)
        active = _doc(status=DomainStatus.ACTIVE)
        revoked = _doc(status=DomainStatus.REVOKED)
        repo.find_by_id = AsyncMock(side_effect=[active, revoked])

        doc, deleted = await svc.delete(DOMAIN_OID, _user(), cascade=True)

        url_service.delete_all_by_domain.assert_awaited_once_with(
            USER_OID, "links.acme.com"
        )
        assert deleted == 42
        assert doc.status == DomainStatus.REVOKED

    @pytest.mark.asyncio
    async def test_cascade_partial_failure_still_revokes(self):
        # bulk delete throws → service swallows, domain still ends REVOKED
        url_service = AsyncMock()
        url_service.delete_all_by_domain = AsyncMock(
            side_effect=RuntimeError("mongo timeout")
        )
        svc, repo, _, _, _ = _build_service(url_service=url_service)
        active = _doc(status=DomainStatus.ACTIVE)
        revoked = _doc(status=DomainStatus.REVOKED)
        repo.find_by_id = AsyncMock(side_effect=[active, revoked])

        doc, deleted = await svc.delete(DOMAIN_OID, _user(), cascade=True)
        assert deleted == 0
        assert doc.status == DomainStatus.REVOKED

    @pytest.mark.asyncio
    async def test_cascade_without_url_service_logs_and_continues(self):
        # Edge case: cascade=True but no url_service wired (test config).
        # Service must not blow up; just log and continue with the revoke.
        svc, repo, _, _, _ = _build_service(url_service=None)
        active = _doc(status=DomainStatus.ACTIVE)
        revoked = _doc(status=DomainStatus.REVOKED)
        repo.find_by_id = AsyncMock(side_effect=[active, revoked])

        doc, deleted = await svc.delete(DOMAIN_OID, _user(), cascade=True)
        assert deleted == 0
        assert doc.status == DomainStatus.REVOKED


class TestUpdateRouting:
    @pytest.mark.asyncio
    async def test_refused_when_disabled(self):
        svc, _, _, _, _ = _build_service(enabled=False)
        with pytest.raises(FeatureDisabledError):
            await svc.update_routing(DOMAIN_OID, _user(), UpdateCustomDomainRequest())

    @pytest.mark.asyncio
    async def test_refused_on_non_active(self):
        # PENDING domains can't be configured — would surprise the owner later.
        svc, repo, _, _, _ = _build_service()
        repo.find_by_id = AsyncMock(return_value=_doc(status=DomainStatus.PENDING))
        with pytest.raises(DomainNotVerifiedError):
            await svc.update_routing(
                DOMAIN_OID,
                _user(),
                UpdateCustomDomainRequest(root_redirect="https://acme.com/"),
            )

    @pytest.mark.asyncio
    async def test_404_when_not_found(self):
        svc, repo, _, _, _ = _build_service()
        repo.find_by_id = AsyncMock(return_value=None)
        with pytest.raises(NotFoundError):
            await svc.update_routing(DOMAIN_OID, _user(), UpdateCustomDomainRequest())

    @pytest.mark.asyncio
    async def test_403_when_not_owner(self):
        svc, repo, _, _, _ = _build_service()
        other_user_doc = _doc(
            status=DomainStatus.ACTIVE,
            owner_id=ObjectId("cccccccccccccccccccccccc"),
        )
        repo.find_by_id = AsyncMock(return_value=other_user_doc)
        with pytest.raises(ForbiddenError):
            await svc.update_routing(DOMAIN_OID, _user(), UpdateCustomDomainRequest())

    @pytest.mark.asyncio
    async def test_happy_path_sets_root_redirect(self):
        svc, repo, _, _, resolver = _build_service()
        active = _doc(status=DomainStatus.ACTIVE)
        # Second find_by_id call returns the post-update doc.
        updated = _doc(status=DomainStatus.ACTIVE)
        updated.root_redirect = "https://acme.com/landing"
        repo.find_by_id = AsyncMock(side_effect=[active, updated])
        repo.update_routing = AsyncMock(return_value=True)
        resolver.invalidate = AsyncMock(return_value=None)

        req = UpdateCustomDomainRequest(root_redirect="https://acme.com/landing")
        result = await svc.update_routing(DOMAIN_OID, _user(), req)

        repo.update_routing.assert_awaited_once()
        ops = repo.update_routing.call_args.args[1]
        # Only the field the caller actually set — partial update semantics.
        assert ops == {"root_redirect": "https://acme.com/landing"}
        # Tenant cache invalidated so middleware sees the new config next request.
        resolver.invalidate.assert_awaited_once_with("links.acme.com")
        assert result.root_redirect == "https://acme.com/landing"

    @pytest.mark.asyncio
    async def test_empty_body_noop(self):
        # No model_fields_set members → service short-circuits before touching
        # Mongo or the cache. Lets clients PATCH with no body to revalidate
        # ownership without side effects.
        svc, repo, _, _, resolver = _build_service()
        active = _doc(status=DomainStatus.ACTIVE)
        repo.find_by_id = AsyncMock(return_value=active)
        repo.update_routing = AsyncMock(return_value=True)
        resolver.invalidate = AsyncMock(return_value=None)

        result = await svc.update_routing(
            DOMAIN_OID, _user(), UpdateCustomDomainRequest()
        )

        repo.update_routing.assert_not_awaited()
        resolver.invalidate.assert_not_awaited()
        assert result is active

    @pytest.mark.asyncio
    async def test_partial_update_only_touches_set_fields(self):
        # Caller sends only `custom_robots_txt`. Other fields must NOT appear
        # in the ops dict — otherwise we'd overwrite root_redirect with None.
        svc, repo, _, _, _ = _build_service()
        active = _doc(status=DomainStatus.ACTIVE)
        repo.find_by_id = AsyncMock(side_effect=[active, active])
        repo.update_routing = AsyncMock(return_value=True)

        req = UpdateCustomDomainRequest(custom_robots_txt="User-agent: *\nAllow: /\n")
        await svc.update_routing(DOMAIN_OID, _user(), req)

        ops = repo.update_routing.call_args.args[1]
        assert set(ops.keys()) == {"custom_robots_txt"}
        assert ops["custom_robots_txt"] == "User-agent: *\nAllow: /\n"

    @pytest.mark.asyncio
    async def test_explicit_null_clears_value(self):
        # PATCH {"root_redirect": null} must set the field to None, distinct
        # from "field omitted" which leaves the current value alone.
        svc, repo, _, _, _ = _build_service()
        active = _doc(status=DomainStatus.ACTIVE)
        repo.find_by_id = AsyncMock(side_effect=[active, active])
        repo.update_routing = AsyncMock(return_value=True)

        # Pydantic accepts None for HttpUrl | None — model_fields_set will
        # still contain the key because the caller explicitly set it.
        req = UpdateCustomDomainRequest(root_redirect=None)
        # Sanity: prove the test setup actually creates the explicit-null shape.
        assert "root_redirect" in req.model_fields_set

        await svc.update_routing(DOMAIN_OID, _user(), req)
        ops = repo.update_routing.call_args.args[1]
        assert ops == {"root_redirect": None}

    @pytest.mark.asyncio
    async def test_audit_log_emitted(self, caplog):
        svc, repo, _, _, _ = _build_service()
        active = _doc(status=DomainStatus.ACTIVE)
        repo.find_by_id = AsyncMock(side_effect=[active, active])
        repo.update_routing = AsyncMock(return_value=True)

        with patch(
            "services.custom_domain_service.log",
            new=MagicMock(),
        ) as mock_log:
            await svc.update_routing(
                DOMAIN_OID,
                _user(),
                UpdateCustomDomainRequest(not_found_redirect="https://acme.com/404"),
            )
            mock_log.info.assert_called_once()
            event_name = mock_log.info.call_args.args[0]
            assert event_name == "audit.domain.routing_updated"
            kwargs = mock_log.info.call_args.kwargs
            assert kwargs["fields"] == ["not_found_redirect"]


class TestDeleteAllForOwner:
    """Account-erasure cascade — raw owner id, no enabled gate."""

    @pytest.mark.asyncio
    async def test_erases_every_domain_even_when_disabled(self):
        url_service = MagicMock()
        url_service.delete_all_by_domain = AsyncMock(return_value=0)
        svc, repo, _, edge, tenant_resolver = _build_service(
            enabled=False, url_service=url_service
        )
        d1 = _doc(fqdn="links.acme.com", domain_id=ObjectId())
        d2 = _doc(fqdn="go.acme.com", domain_id=ObjectId())
        repo.count_by_owner = AsyncMock(return_value=2)
        repo.list_by_owner = AsyncMock(side_effect=[[d1, d2], []])

        removed = await svc.delete_all_for_owner(USER_OID)

        assert removed == 2
        assert repo.delete_by_id.await_count == 2
        # Erasure mode: BLOCKED links on the owner's own fqdns are retained
        # (scrubbed by the URL layer) — unlike the interactive revoke cascade.
        url_service.delete_all_by_domain.assert_any_await(
            USER_OID, "links.acme.com", retain_blocked=True
        )
        url_service.delete_all_by_domain.assert_any_await(
            USER_OID, "go.acme.com", retain_blocked=True
        )
        edge.announce_revoked.assert_any_await("links.acme.com")
        tenant_resolver.invalidate.assert_any_await("go.acme.com")

    @pytest.mark.asyncio
    async def test_no_domains_is_a_noop(self):
        svc, repo, _, edge, _ = _build_service()
        repo.count_by_owner = AsyncMock(return_value=0)
        repo.list_by_owner = AsyncMock(return_value=[])
        assert await svc.delete_all_for_owner(USER_OID) == 0
        repo.delete_by_id.assert_not_awaited()
        edge.announce_revoked.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_eviction_raises_and_keeps_the_doc(self):
        svc, repo, _, edge, _ = _build_service()
        edge.announce_revoked = AsyncMock(return_value=False)
        repo.count_by_owner = AsyncMock(return_value=1)
        repo.list_by_owner = AsyncMock(side_effect=[[_doc()], []])
        with pytest.raises(AppError):
            await svc.delete_all_for_owner(USER_OID)
        # Doc survives so the re-queued sweep has something to retry against.
        repo.delete_by_id.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_repo_failure_propagates(self):
        svc, repo, _, _, _ = _build_service()
        repo.count_by_owner = AsyncMock(return_value=1)
        repo.list_by_owner = AsyncMock(side_effect=[[_doc()], []])
        repo.delete_by_id = AsyncMock(side_effect=RuntimeError("mongo down"))
        with pytest.raises(RuntimeError):
            await svc.delete_all_for_owner(USER_OID)

    @pytest.mark.asyncio
    async def test_noop_delete_raises_instead_of_spinning(self):
        """A delete that removes nothing would re-list the same page forever
        (page zero is re-read each pass) — it must raise, not loop."""
        from errors import AppError

        svc, repo, _, _, _ = _build_service()
        repo.count_by_owner = AsyncMock(return_value=1)
        repo.list_by_owner = AsyncMock(return_value=[_doc()])
        repo.delete_by_id = AsyncMock(return_value=False)
        with pytest.raises(AppError):
            await svc.delete_all_for_owner(USER_OID)
        # Exactly one attempt — the loop never got a second spin.
        repo.delete_by_id.assert_awaited_once_with(DOMAIN_OID)

    @pytest.mark.asyncio
    async def test_pass_cap_raises_when_pages_never_drain(self):
        """Deletes report success but the listing never shrinks (replica
        lag, repo drift): the absolute pass cap turns it into a failure."""
        from errors import AppError

        svc, repo, _, _, _ = _build_service()
        repo.count_by_owner = AsyncMock(return_value=1)
        repo.list_by_owner = AsyncMock(return_value=[_doc()])
        repo.delete_by_id = AsyncMock(return_value=True)
        with pytest.raises(AppError):
            await svc.delete_all_for_owner(USER_OID)
        # count=1 ⇒ (1 // 50) + 2 = 2 passes, then the loud stop.
        assert repo.list_by_owner.await_count == 2
