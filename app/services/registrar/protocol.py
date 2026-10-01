"""HostnameRegistrar protocol — abstracts pre-verification edge registration.

Some edge platforms (Cloudflare for SaaS) need the hostname registered
*before* DNS verification can succeed, since the platform is what runs the
DCV check and issues the cert. Other platforms (Caddy on-demand TLS) need
nothing — verification happens on first traffic and cert issuance is lazy.

Splitting this off from ``DomainVerifier`` keeps both contracts honest:
verifiers are pure proof-checks; the registrar owns "tell the edge this
hostname exists" and returns the per-backend bookkeeping the service has
to persist (CF hostname id, DCV instructions surfaced to the user).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class RegistrationResult:
    """Outcome of a single ``register`` call.

    ``backend_id`` is the foreign key the registrar needs to look up the
    hostname later (CF hostname id, etc.). ``instructions`` is the user-
    facing DNS guidance — exact records to add — that the dashboard shows
    after a successful create. ``backend_metadata`` carries any extra fields
    the service should persist (cf_status, cf_ssl_status snapshots).
    """

    backend_id: str | None
    instructions: list[dict[str, Any]] = field(default_factory=list)
    backend_metadata: dict[str, Any] = field(default_factory=dict)


class HostnameRegistrar(Protocol):
    """Strategy interface for announcing a new hostname to the edge plane."""

    async def register(
        self, fqdn: str, *, dcv_method: str | None = None
    ) -> RegistrationResult:
        """Register *fqdn* with the edge platform.

        Implementations MAY raise on backend errors — the service layer
        wraps the call so a failed registration rolls back the Mongo
        insert. ``dcv_method`` is the verification strategy chosen by the
        user; ignored by backends that don't need it (NoOp).

        Teardown is owned by ``EdgeProvisioner.announce_revoked`` — for
        CF SaaS the same backend implements both protocols and routes
        the revoke through ``cf.delete_custom_hostname``. Splitting the
        teardown across two protocol methods would duplicate the call
        site without adding value.
        """
        ...
