"""CF SaaS backend — one class fills Registrar + Verifier + EdgeProvisioner.
Single class because the CF hostname id ties all three operations together."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import httpx

from app.errors import CloudflareAPIError
from app.infrastructure.cloudflare_client import CloudflareClient
from app.infrastructure.logging import get_logger
from app.services.edge_provisioner.protocol import EdgeProvisioner
from app.services.registrar.protocol import HostnameRegistrar, RegistrationResult
from app.services.verifiers.protocol import DomainVerifier, VerificationResult

if TYPE_CHECKING:
    from app.repositories.custom_domain_repository import CustomDomainRepository

log = get_logger(__name__)


_DCV_METHOD_MAP = {
    "cf_delegated_dcv": "txt",
    "cf_http_dcv": "http",
}


class CfSaasBackend(HostnameRegistrar, DomainVerifier, EdgeProvisioner):
    def __init__(
        self,
        *,
        cf_client: CloudflareClient,
        custom_domain_repo: CustomDomainRepository,
        cname_target: str,
        dcv_delegation_target: str,
    ) -> None:
        self._cf = cf_client
        self._repo = custom_domain_repo
        self._cname_target = cname_target.strip(".")
        self._dcv_delegation_target = dcv_delegation_target.strip(".")

    # ── HostnameRegistrar ───────────────────────────────────────────────

    async def register(
        self, fqdn: str, *, dcv_method: str | None = None
    ) -> RegistrationResult:
        method = dcv_method or "cf_http_dcv"
        cf_method = _DCV_METHOD_MAP.get(method, "http")
        result = await self._cf.create_custom_hostname(fqdn, dcv_method=cf_method)
        instructions = self._build_dns_instructions(fqdn, method)
        if result.ownership_verification:
            instructions.append(
                {
                    "type": result.ownership_verification["type"].upper(),
                    "name": result.ownership_verification["name"],
                    "value": result.ownership_verification["value"],
                    "purpose": "proves domain ownership",
                }
            )
        return RegistrationResult(
            backend_id=result.id,
            instructions=instructions,
            backend_metadata={
                "cf_status": result.status,
                "cf_ssl_status": result.ssl_status,
            },
        )

    # ── DomainVerifier ──────────────────────────────────────────────────

    async def verify(self, fqdn: str, token: str | None = None) -> VerificationResult:
        if not token:
            return VerificationResult(
                verified=False,
                reason="missing cf_hostname_id; backend cannot poll CF",
            )

        try:
            result = await self._cf.get_custom_hostname(token)
        except CloudflareAPIError as exc:
            return VerificationResult(verified=False, reason=str(exc))
        except httpx.HTTPError as exc:
            return VerificationResult(verified=False, reason=f"network error: {exc}")

        # Force re-probe so user's click doesn't sit in CF's 15-min backoff.
        # Only HTTP DCV runs probes; delegated DCV auto-validates.
        if result.ssl_status != "active" and result.ssl_method == "http":
            with contextlib.suppress(CloudflareAPIError):
                result = await self._cf.recheck_custom_hostname(
                    token, dcv_method=result.ssl_method
                )

        verified = result.status == "active" and result.ssl_status == "active"
        if verified:
            return VerificationResult(verified=True)

        if result.verification_errors:
            reason = "; ".join(result.verification_errors)
        else:
            reason = f"status={result.status}, ssl_status={result.ssl_status}"
        return VerificationResult(verified=False, reason=reason)

    # ── EdgeProvisioner ─────────────────────────────────────────────────

    async def announce_revoked(self, fqdn: str) -> bool:
        # Protocol: never raise. Failures return False so the orchestrator
        # stamps eviction_pending and the sync worker retries.
        backend_id, status = await self._resolve_backend_id(fqdn)
        if status == "absent":
            log.info("cf_saas_revocation_already_absent", fqdn=fqdn)
            return True
        if status == "lookup_failed" or backend_id is None:
            return False

        try:
            await self._cf.delete_custom_hostname(backend_id)
        except CloudflareAPIError as exc:
            log.warning(
                "cf_saas_revocation_rejected",
                fqdn=fqdn,
                backend_id=backend_id,
                error=str(exc),
            )
            return False
        except Exception as exc:
            log.exception(
                "cf_saas_revocation_unexpected_error",
                fqdn=fqdn,
                backend_id=backend_id,
                error=str(exc),
            )
            return False

        log.info(
            "cf_saas_revocation_announced",
            fqdn=fqdn,
            backend_id=backend_id,
        )
        return True

    async def _resolve_backend_id(self, fqdn: str) -> tuple[str | None, str]:
        """Returns (id, status) — status is "resolved" | "absent" | "lookup_failed"."""
        try:
            doc = await self._repo.find_by_fqdn(fqdn)
        except Exception as exc:
            log.warning(
                "cf_saas_revoke_doc_lookup_failed",
                fqdn=fqdn,
                error=str(exc),
            )
            doc = None
        if doc is not None and doc.cf_hostname_id:
            return doc.cf_hostname_id, "resolved"

        try:
            lookup = await self._cf.find_hostname_by_fqdn(fqdn)
        except CloudflareAPIError as exc:
            log.warning(
                "cf_saas_revoke_lookup_failed",
                fqdn=fqdn,
                error=str(exc),
            )
            return None, "lookup_failed"
        if lookup is None:
            return None, "absent"
        return lookup.id, "resolved"

    # ── helpers ─────────────────────────────────────────────────────────

    def _build_dns_instructions(
        self, fqdn: str, dcv_method: str
    ) -> list[dict[str, str]]:
        records: list[dict[str, str]] = [
            {
                "type": "CNAME",
                "name": fqdn,
                "value": self._cname_target,
                "purpose": "routes traffic to spoo.me",
            }
        ]
        if dcv_method == "cf_delegated_dcv" and self._dcv_delegation_target:
            records.append(
                {
                    "type": "CNAME",
                    "name": f"_acme-challenge.{fqdn}",
                    "value": f"{fqdn}.{self._dcv_delegation_target}",
                    "purpose": "auto-renews TLS certificate",
                }
            )
        return records
