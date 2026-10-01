"""Mock DCV backend — local-dev stand-in for CfSaasBackend.

Wired by ``CUSTOM_DOMAINS_MOCK_DCV=true`` (dev only). Fills the same three
protocol slots as CfSaasBackend (registrar, verifier, edge provisioner) so
the service layer is none the wiser. ``register`` returns instructions with
the exact shape production serves — the routing CNAME plus the Cloudflare
ownership TXT (``_cf-custom-hostname.<fqdn>``) — and ``verify`` succeeds
unconditionally, so the dashboard walks PENDING → ACTIVE without CF creds.
"""

from __future__ import annotations

import uuid

from app.infrastructure.logging import get_logger
from app.services.edge_provisioner.protocol import EdgeProvisioner
from app.services.registrar.protocol import HostnameRegistrar, RegistrationResult
from app.services.verifiers.protocol import DomainVerifier, VerificationResult

log = get_logger(__name__)


class MockDcvBackend(HostnameRegistrar, DomainVerifier, EdgeProvisioner):
    def __init__(self, *, cname_target: str) -> None:
        self._cname_target = cname_target.strip(".")

    # ── HostnameRegistrar ───────────────────────────────────────────────

    async def register(
        self, fqdn: str, *, dcv_method: str | None = None
    ) -> RegistrationResult:
        # Same two records prod hands back for a CF SaaS registration:
        # the traffic CNAME and CF's ownership_verification TXT.
        instructions = [
            {
                "type": "CNAME",
                "name": fqdn,
                "value": self._cname_target,
                "purpose": "routes traffic to spoo.me",
            },
            {
                "type": "TXT",
                "name": f"_cf-custom-hostname.{fqdn}",
                "value": str(uuid.uuid4()),
                "purpose": "proves domain ownership",
            },
        ]
        log.info("mock_dcv_registered", fqdn=fqdn, dcv_method=dcv_method)
        return RegistrationResult(
            backend_id=f"mock-{uuid.uuid4()}",
            instructions=instructions,
            backend_metadata={
                "cf_status": "pending",
                "cf_ssl_status": "pending_validation",
            },
        )

    # ── DomainVerifier ──────────────────────────────────────────────────

    async def verify(self, fqdn: str, token: str | None = None) -> VerificationResult:
        log.info("mock_dcv_verified", fqdn=fqdn)
        return VerificationResult(verified=True)

    # ── EdgeProvisioner ─────────────────────────────────────────────────

    async def announce_revoked(self, fqdn: str) -> bool:
        log.info("mock_dcv_revocation_announced", fqdn=fqdn)
        return True
