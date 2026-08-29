"""Custom-domain lifecycle orchestrator. Owns state machine, quotas, and
audit.domain.* events. Mutations gated by settings.enabled."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from config import CustomDomainSettings
from errors import (
    DomainAlreadyRegisteredError,
    DomainBlocklistedError,
    DomainNotVerifiedError,
    DomainQuotaExceededError,
    FeatureDisabledError,
    ForbiddenError,
    InvalidDomainTransitionError,
    NotFoundError,
)
from infrastructure.logging import get_logger
from repositories.blocked_domain_repository import BlockedDomainRepository
from repositories.custom_domain_repository import CustomDomainRepository
from schemas.dto.requests.custom_domain import (
    CreateCustomDomainRequest,
    ListCustomDomainsQuery,
    UpdateCustomDomainRequest,
)
from schemas.enums.domain_status import DomainStatus, VerificationMethod
from schemas.models.custom_domain import LEGAL_TRANSITIONS, CustomDomainDoc
from services.dns_preflight import check_cname, uses_cloudflare_dns
from services.edge_provisioner.protocol import EdgeProvisioner
from services.registrar.protocol import HostnameRegistrar
from services.tenant_resolver.protocol import TenantResolver
from services.verifiers.protocol import DomainVerifier, VerificationResult
from shared.url_utils import is_registrable_apex

if TYPE_CHECKING:
    import redis.asyncio as aioredis

    from dependencies.auth import CurrentUser
    from services.url_service import UrlService

log = get_logger(__name__)


class CustomDomainService:
    """Orchestrator for the custom-domain feature."""

    def __init__(
        self,
        repo: CustomDomainRepository,
        verifiers: dict[VerificationMethod, DomainVerifier],
        edge_provisioner: EdgeProvisioner,
        registrar: HostnameRegistrar,
        settings: CustomDomainSettings,
        tenant_resolver: TenantResolver | None = None,
        blocked_domain_repo: BlockedDomainRepository | None = None,
        redis_client: aioredis.Redis | None = None,
        preflight_cname_target: str | None = None,
        url_service: UrlService | None = None,
    ) -> None:
        self._repo = repo
        self._verifiers = verifiers
        self._edge = edge_provisioner
        self._registrar = registrar
        self._settings = settings
        self._tenant_resolver = tenant_resolver
        self._blocked_repo = blocked_domain_repo
        self._redis = redis_client
        # None = preflight off (tests, self-host LE).
        self._preflight_cname_target = preflight_cname_target
        # None = cascade delete unavailable (tests). Production wiring sets this.
        self._url_service = url_service

    # ── Public API ───────────────────────────────────────────────────

    async def create(
        self,
        request: CreateCustomDomainRequest,
        user: CurrentUser,
    ) -> CustomDomainDoc:
        """Register a new custom domain. Doc is born in PENDING state."""
        self._require_enabled()

        await self._enforce_blocklist(request.fqdn)
        await self._enforce_uniqueness(request.fqdn)
        await self._enforce_per_user_quota(user.user_id)

        method = self._pick_verification_method(request.fqdn)
        if method not in self._verifiers:
            raise InvalidDomainTransitionError(
                f"unsupported verification method: {method.value}"
            )

        now = datetime.now(timezone.utc)
        setup_notes = await self._build_setup_notes(request.fqdn)
        doc = CustomDomainDoc(
            fqdn=request.fqdn,
            owner_id=user.user_id,
            status=DomainStatus.PENDING,
            verification_method=method,
            verification_token=str(uuid.uuid4()),
            is_system_default=False,
            created_at=now,
            setup_notes=setup_notes,
        )
        try:
            new_id = await self._repo.insert(doc.to_mongo())
        except DuplicateKeyError:
            # Service-level uniqueness check already caught the common cases;
            # this is the index backstop for concurrent insert races.
            raise DomainAlreadyRegisteredError(
                f"{request.fqdn} is already registered."
            ) from None

        try:
            registration = await self._registrar.register(
                request.fqdn, dcv_method=method.value
            )
        except Exception as exc:
            # Roll back Mongo so a CF failure doesn't block re-create.
            try:
                await self._repo.delete_by_id(new_id)
            except Exception as rollback_exc:
                log.exception(
                    "audit.domain.registration_rollback_failed",
                    fqdn=request.fqdn,
                    domain_id=str(new_id),
                    owner_id=str(user.user_id),
                    rollback_error=str(rollback_exc),
                )
            log.warning(
                "audit.domain.registration_failed",
                fqdn=request.fqdn,
                domain_id=str(new_id),
                owner_id=str(user.user_id),
                verification_method=method.value,
                error=str(exc),
            )
            raise

        if (
            registration.backend_id is not None
            or registration.backend_metadata
            or registration.instructions
        ):
            await self._repo.update_edge_metadata(
                new_id,
                cf_hostname_id=registration.backend_id,
                cf_status=registration.backend_metadata.get("cf_status"),
                cf_ssl_status=registration.backend_metadata.get("cf_ssl_status"),
                dns_instructions=registration.instructions or None,
            )

        log.info(
            "audit.domain.created",
            fqdn=request.fqdn,
            domain_id=str(new_id),
            owner_id=str(user.user_id),
            verification_method=method.value,
        )

        created = await self._repo.find_by_id(new_id)
        if created is None:  # pragma: no cover
            raise NotFoundError(f"domain {new_id} vanished after insert")
        return created

    async def verify(
        self,
        domain_id: ObjectId,
        user: CurrentUser,
    ) -> CustomDomainDoc:
        """Dispatch the verifier. Success → ACTIVE. Failure records reason."""
        self._require_enabled()

        doc = await self._load_owned(domain_id, user)
        await self._enforce_verify_attempts_quota(domain_id)

        # DNS preflight short-circuits CF API calls so unpropagated domains
        # don't trigger CF's 15-min backoff. Soft failure: recorded as
        # last_verification_error, not raised.
        if self._preflight_cname_target:
            preflight = await check_cname(doc.fqdn, self._preflight_cname_target)
            if not preflight.ok:
                await self._repo.update_status(
                    doc.id,
                    doc.status,
                    last_verification_error=preflight.reason,
                )
                log.info(
                    "audit.domain.preflight_failed",
                    fqdn=doc.fqdn,
                    domain_id=str(doc.id),
                    owner_id=str(user.user_id),
                    reason=preflight.reason,
                )
                refreshed = await self._repo.find_by_id(doc.id)
                return refreshed or doc

        verifier = self._verifiers.get(doc.verification_method)
        if verifier is None:
            raise InvalidDomainTransitionError(
                f"no verifier wired for {doc.verification_method.value}"
            )

        result: VerificationResult = await verifier.verify(
            doc.fqdn, self._verifier_token(doc)
        )

        if result.verified:
            await self._transition(
                doc,
                DomainStatus.ACTIVE,
                last_verification_error=None,
                bump_last_verified_at=True,
            )
            await self._invalidate_cache(doc.fqdn)
            log.info(
                "audit.domain.verified",
                fqdn=doc.fqdn,
                domain_id=str(doc.id),
                owner_id=str(user.user_id),
                method=doc.verification_method.value,
                last_verification_error=None,
            )
        else:
            # Stay in current state; record reason. No auto-suspend on a
            # single failed click — sync worker (PR5) owns that.
            await self._repo.update_status(
                doc.id,
                doc.status,
                last_verification_error=result.reason,
            )
            log.info(
                "audit.domain.verified",
                fqdn=doc.fqdn,
                domain_id=str(doc.id),
                owner_id=str(user.user_id),
                method=doc.verification_method.value,
                last_verification_error=result.reason,
            )

        refreshed = await self._repo.find_by_id(doc.id)
        if refreshed is None:  # pragma: no cover
            raise NotFoundError(f"domain {doc.id} vanished after verify")
        return refreshed

    async def list_by_owner(
        self,
        user: CurrentUser,
        query: ListCustomDomainsQuery,
    ) -> tuple[list[CustomDomainDoc], int]:
        """Return (page, total_count) for caller's domains. Reads bypass flag
        gate so owners can still see their state during a rollback."""
        skip = (query.page - 1) * query.page_size
        items = await self._repo.list_by_owner(
            user.user_id, skip=skip, limit=query.page_size
        )
        total = await self._repo.count_by_owner(user.user_id)
        return items, total

    async def update_routing(
        self,
        domain_id: ObjectId,
        user: CurrentUser,
        request: UpdateCustomDomainRequest,
    ) -> CustomDomainDoc:
        """Update the per-domain routing config (root/404 redirects + robots.txt).

        Partial update: only fields the caller explicitly set are touched.
        Empty body is a no-op. Refuses non-ACTIVE domains — surprising UX
        otherwise (you set a redirect on a PENDING domain, then forget, then
        verify weeks later, and suddenly traffic redirects to a destination
        you no longer remember).
        """
        self._require_enabled()

        doc = await self._load_owned(domain_id, user)
        if doc.status != DomainStatus.ACTIVE:
            raise DomainNotVerifiedError(
                f"{doc.fqdn} isn't active yet. Verify the domain before "
                f"configuring routing."
            )

        ops: dict = {}
        # model_fields_set distinguishes "field omitted" (don't touch) from
        # "field set to None" (clear stored value). HttpUrl needs str()
        # coercion before persistence.
        if "root_redirect" in request.model_fields_set:
            ops["root_redirect"] = (
                str(request.root_redirect) if request.root_redirect else None
            )
        if "not_found_redirect" in request.model_fields_set:
            ops["not_found_redirect"] = (
                str(request.not_found_redirect) if request.not_found_redirect else None
            )
        if "custom_robots_txt" in request.model_fields_set:
            ops["custom_robots_txt"] = request.custom_robots_txt or None

        if not ops:
            return doc

        updated = await self._repo.update_routing(doc.id, ops)
        if not updated:
            # Doc was deleted or its _id changed between our load and the
            # update — surface this rather than logging a successful audit
            # event for a write that didn't happen.
            raise NotFoundError(
                f"{doc.fqdn} was modified or deleted concurrently; retry."
            )
        # Tenant cache holds the routing fields; without invalidate, the next
        # ``tenant_cache_ttl`` seconds keep serving the old config.
        await self._invalidate_cache(doc.fqdn)

        log.info(
            "audit.domain.routing_updated",
            fqdn=doc.fqdn,
            domain_id=str(doc.id),
            owner_id=str(user.user_id),
            fields=list(ops.keys()),
        )

        refreshed = await self._repo.find_by_id(doc.id)
        return refreshed or doc

    async def delete(
        self,
        domain_id: ObjectId,
        user: CurrentUser,
        *,
        cascade: bool = False,
    ) -> tuple[CustomDomainDoc, int]:
        """Revoke a custom domain. REVOKED is terminal.

        When ``cascade=True``, bulk-deletes all URLs owned by the user on the
        revoked fqdn. Returns ``(doc, urls_deleted)`` so callers can surface
        the deletion count.

        Order: transition to REVOKED FIRST so concurrent shortens can't sneak
        in. Then bulk delete (best-effort — partial failure leaves orphans
        for the PR5 GC worker). Then announce eviction + invalidate cache.
        """
        self._require_enabled()

        doc = await self._load_owned(domain_id, user)
        await self._transition(doc, DomainStatus.REVOKED)

        urls_deleted = 0
        if cascade:
            if self._url_service is None:
                log.error(
                    "audit.domain.cascade_unavailable",
                    fqdn=doc.fqdn,
                    domain_id=str(doc.id),
                )
            else:
                try:
                    urls_deleted = await self._url_service.delete_all_by_domain(
                        user.user_id, doc.fqdn
                    )
                except Exception as exc:
                    log.warning(
                        "audit.domain.cascade_partial",
                        fqdn=doc.fqdn,
                        domain_id=str(doc.id),
                        owner_id=str(user.user_id),
                        error=str(exc),
                    )

        await self._announce_eviction(doc, kind="revoked")
        await self._invalidate_cache(doc.fqdn)

        log.info(
            "audit.domain.revoked",
            fqdn=doc.fqdn,
            domain_id=str(doc.id),
            owner_id=str(user.user_id),
            cascade=cascade,
            urls_deleted=urls_deleted,
        )

        refreshed = await self._repo.find_by_id(doc.id)
        return refreshed or doc, urls_deleted

    async def remove_revoked(
        self,
        domain_id: ObjectId,
        user: CurrentUser,
    ) -> CustomDomainDoc:
        """Hard-delete a REVOKED domain doc to free the caller's slot.

        Revoke is soft (audit trail preserved), so the slot stays occupied
        until the user explicitly reclaims it via this call. Refuses any
        non-REVOKED status so the caller can't accidentally nuke an active
        domain — they must Revoke first, then Remove.
        """
        self._require_enabled()

        doc = await self._load_owned(domain_id, user)
        if doc.status != DomainStatus.REVOKED:
            raise InvalidDomainTransitionError(
                "Only revoked domains can be removed. Revoke this domain first."
            )

        deleted = await self._repo.delete_by_id(doc.id)
        if not deleted:
            raise NotFoundError("Domain not found.")

        log.info(
            "audit.domain.removed",
            fqdn=doc.fqdn,
            domain_id=str(doc.id),
            owner_id=str(user.user_id),
        )
        return doc

    async def assert_owned(
        self,
        user: CurrentUser,
        fqdn: str,
    ) -> CustomDomainDoc:
        """Find domain by fqdn, raise 403/404. No status check.

        Used by bulk URL delete which should work on revoked/suspended domains
        too (cleanup path)."""
        doc = await self._repo.find_by_fqdn(fqdn)
        if doc is None:
            raise NotFoundError(f"{fqdn} is not registered.")
        if doc.owner_id != user.user_id:
            raise ForbiddenError("You do not own this domain.")
        return doc

    async def assert_owned_and_active(
        self,
        user: CurrentUser,
        fqdn: str,
    ) -> CustomDomainDoc:
        """Find domain, assert ownership + ACTIVE. Used by shorten flow."""
        doc = await self.assert_owned(user, fqdn)
        if doc.status != DomainStatus.ACTIVE:
            raise DomainNotVerifiedError(
                f"{fqdn} isn't verified yet. Set up DNS and verify it first."
            )
        return doc

    # ── PR5 sync worker helpers ──────────────────────────────────────

    async def reverify_active(
        self, batch_size: int | None = None
    ) -> list[tuple[CustomDomainDoc, VerificationResult]]:
        """Re-check ACTIVE domains older than freshness window. Worker (PR5)
        handles ACTIVE→SUSPENDED on N consecutive fails."""
        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=self._settings.max_verify_age_seconds
        )
        limit = batch_size or self._settings.reverify_batch_size
        stale = await self._repo.find_stale_active(cutoff, limit)

        out: list[tuple[CustomDomainDoc, VerificationResult]] = []
        for doc in stale:
            verifier = self._verifiers.get(doc.verification_method)
            if verifier is None:
                continue
            result = await verifier.verify(doc.fqdn, self._verifier_token(doc))
            if result.verified:
                await self._repo.update_status(
                    doc.id,
                    DomainStatus.ACTIVE,
                    last_verification_error=None,
                    bump_last_verified_at=True,
                )
            else:
                await self._repo.update_status(
                    doc.id,
                    doc.status,
                    last_verification_error=result.reason,
                )
            out.append((doc, result))
        return out

    async def suspend(
        self, domain_id: ObjectId, reason: str, *, actor: str = "worker"
    ) -> None:
        """Force a domain into SUSPENDED. Used by the reverify worker."""
        doc = await self._repo.find_by_id(domain_id)
        if doc is None:
            return
        await self._transition(
            doc,
            DomainStatus.SUSPENDED,
            last_verification_error=reason,
        )
        await self._announce_eviction(doc, kind="suspended")
        await self._invalidate_cache(doc.fqdn)
        log.info(
            "audit.domain.suspended",
            fqdn=doc.fqdn,
            domain_id=str(doc.id),
            owner_id=str(doc.owner_id),
            reason=reason,
            suspending_actor=actor,
        )

    # ── Internal: state machine, quotas, blocklist, cache ────────────

    def _pick_verification_method(self, fqdn: str) -> VerificationMethod:
        """Backend picks DCV method. CF SaaS → cf_http_dcv. Self-host LE →
        a_record for apex, cname otherwise."""
        if VerificationMethod.CF_HTTP_DCV in self._verifiers:
            return VerificationMethod.CF_HTTP_DCV
        if is_registrable_apex(fqdn) and VerificationMethod.A_RECORD in self._verifiers:
            return VerificationMethod.A_RECORD
        return VerificationMethod.CNAME

    @staticmethod
    def _verifier_token(doc: CustomDomainDoc) -> str | None:
        # CF backends key off the cf_hostname_id; DNS verifiers off the
        # per-domain UUID. Prefix check auto-routes new cf_* methods.
        if doc.verification_method.value.startswith("cf_"):
            return doc.cf_hostname_id
        return doc.verification_token

    def _require_enabled(self) -> None:
        if not self._settings.enabled:
            raise FeatureDisabledError(
                feature="custom_domains",
                message="Custom domains aren't available yet.",
            )

    async def _invalidate_cache(self, fqdn: str) -> None:
        """Best-effort tenant-cache eviction. Staleness is degraded UX, not data loss."""
        if self._tenant_resolver is None:
            return
        try:
            await self._tenant_resolver.invalidate(fqdn)
        except Exception as exc:
            log.warning(
                "tenant_cache_invalidate_failed",
                fqdn=fqdn,
                error=str(exc),
            )

    async def _announce_eviction(self, doc: CustomDomainDoc, *, kind: str) -> None:
        """Tell edge to drop fqdn. Stamps eviction_pending on failure for
        PR5 sync worker to retry. ``kind`` only affects the error message."""
        ok = await self._edge.announce_revoked(doc.fqdn)
        await self._repo.set_eviction_pending(
            doc.id,
            pending=not ok,
            error=None if ok else f"caddy {kind} eviction failed",
        )

    async def get_owned_by_id(
        self,
        domain_id: ObjectId,
        user: CurrentUser,
    ) -> CustomDomainDoc:
        """Public read for the caller's domain by id. 403/404 same as mutations.

        Used by the detail view, refresh-after-verify, and auto-poll. Bypasses
        the master `enabled` flag so owners can see their state during rollback.
        """
        return await self._load_owned(domain_id, user)

    async def _load_owned(
        self, domain_id: ObjectId, user: CurrentUser
    ) -> CustomDomainDoc:
        doc = await self._repo.find_by_id(domain_id)
        if doc is None:
            raise NotFoundError("Domain not found.")
        if doc.owner_id != user.user_id:
            raise ForbiddenError("You do not own this domain.")
        return doc

    async def _transition(
        self,
        doc: CustomDomainDoc,
        new_status: DomainStatus,
        *,
        last_verification_error: str | None = None,
        bump_last_verified_at: bool = False,
    ) -> None:
        # Self-loop = idempotent retry. Skip legality; still bump fields.
        if doc.status == new_status:
            await self._repo.update_status(
                doc.id,
                new_status,
                last_verification_error=last_verification_error,
                bump_last_verified_at=bump_last_verified_at,
            )
            return
        legal = LEGAL_TRANSITIONS.get(doc.status, frozenset())
        if new_status not in legal:
            raise InvalidDomainTransitionError(
                f"illegal transition {doc.status.value} -> {new_status.value}"
            )
        await self._repo.update_status(
            doc.id,
            new_status,
            last_verification_error=last_verification_error,
            bump_last_verified_at=bump_last_verified_at,
        )

    async def _enforce_uniqueness(self, fqdn: str) -> None:
        # REVOKED docs are terminal and don't reserve the fqdn — same posture
        # as Vercel/Netlify/CF SaaS. DCV at register-time is the security
        # gate against takeover.
        existing = await self._repo.find_blocking_by_fqdn(fqdn)
        if existing is not None:
            raise DomainAlreadyRegisteredError(
                f"{fqdn} is registered to another account."
            )

    async def _enforce_per_user_quota(self, owner_id: ObjectId) -> None:
        current = await self._repo.count_by_owner(owner_id)
        if current >= self._settings.max_per_user:
            cap = self._settings.max_per_user
            suffix = "domain" if cap == 1 else "domains"
            raise DomainQuotaExceededError(
                f"You already have {cap} custom {suffix} on this account. "
                f"Remove a revoked one, or revoke an active one and then "
                f"remove it, before adding another."
            )

    async def _enforce_verify_attempts_quota(self, domain_id: ObjectId) -> None:
        if self._redis is None:
            return
        key = f"domain_verify_attempts:{domain_id}"
        try:
            count = await self._redis.incr(key)
            if count == 1:
                await self._redis.expire(key, 3600)
        except Exception as exc:
            log.warning("verify_quota_redis_error", error=str(exc))
            return
        if count > self._settings.verify_attempts_per_hour:
            raise DomainQuotaExceededError(
                "Too many verification attempts. Try again in an hour."
            )

    async def _build_setup_notes(self, fqdn: str) -> list[str]:
        # NS lookup is only meaningful on the CF SaaS path; grey-cloud is a
        # CF-SaaS-specific gotcha. Skip on self-host LE.
        if not self._preflight_cname_target:
            return []
        notes: list[str] = []
        try:
            if await uses_cloudflare_dns(fqdn):
                notes.append(
                    "Cloudflare DNS detected. Set the record to DNS only "
                    "(grey cloud icon), not Proxied (orange cloud), or "
                    "verification will fail."
                )
        except Exception as exc:
            log.warning("setup_notes_ns_lookup_failed", fqdn=fqdn, error=str(exc))
        return notes

    async def _enforce_blocklist(self, fqdn: str) -> None:
        # Live Mongo — operator can add an abuse domain via mongosh and the
        # next create() honours it without restart.
        if self._blocked_repo is None:
            return
        if await self._blocked_repo.is_blocked(fqdn):
            raise DomainBlocklistedError("This domain isn't available.")
