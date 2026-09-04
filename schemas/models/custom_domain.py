"""Document model for the ``custom_domains`` collection. One row per fqdn
(system default + user-registered). DNS resolution lives in verifiers,
never in Pydantic validators."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from schemas.enums.domain_status import DomainStatus, VerificationMethod
from schemas.models.base import MongoBaseModel, PyObjectId
from shared.url_utils import normalise_fqdn


class CustomDomainDoc(MongoBaseModel):
    """Document model for the ``custom_domains`` collection."""

    fqdn: str
    owner_id: PyObjectId
    status: DomainStatus = DomainStatus.PENDING
    verification_method: VerificationMethod
    # UUID stamped on every doc for shape uniformity; only the TXT verifier
    # actually consumes it.
    verification_token: str | None = None
    is_system_default: bool = False
    # Newest domains beyond the plan's limit stop serving until it grows.
    paused_by_limit: bool = False

    created_at: datetime
    updated_at: datetime | None = None
    last_verified_at: datetime | None = None
    last_verification_error: str | None = None

    # Set when revoke/suspend fired but the edge didn't ack the eviction.
    # Orthogonal to status; sync worker scans these and retries.
    eviction_pending: bool = False
    last_eviction_error: str | None = None

    # CF SaaS bookkeeping. None on self-host LE deployments.
    cf_hostname_id: str | None = None
    cf_status: str | None = None
    cf_ssl_status: str | None = None

    # DNS records + setup hints surfaced to the user. Stamped at create
    # time so the dashboard reads from the doc, not the backend.
    dns_instructions: list[dict[str, str]] = Field(default_factory=list)
    setup_notes: list[str] = Field(default_factory=list)

    # Per-domain routing config (PR4.5). All optional; absence preserves the
    # PR4 behavior (404 on root + non-alias paths, default robots.txt
    # `Disallow: /`). Read by `middleware/tenant.py` from the cached
    # `TenantInfo` — no extra DB hits per request. Only honored when
    # ``status == ACTIVE`` so revoked/suspended domains don't keep redirecting.
    root_redirect: str | None = None
    not_found_redirect: str | None = None
    custom_robots_txt: str | None = Field(default=None, max_length=4096)

    @field_validator("fqdn", mode="before")
    @classmethod
    def _normalise(cls, v: Any) -> str:
        return normalise_fqdn(v)

    @field_validator(
        "root_redirect", "not_found_redirect", "custom_robots_txt", mode="before"
    )
    @classmethod
    def _empty_to_none(cls, v: Any) -> Any:
        # Mongo stores `None` for "unset"; an empty string from the API would
        # otherwise sneak through as a real value that the middleware treats
        # as truthy (`if tenant.root_redirect:` short-circuits, etc.).
        if isinstance(v, str) and not v.strip():
            return None
        return v


# State machine. VERIFYING reserved for future async verification flows.
LEGAL_TRANSITIONS: dict[DomainStatus, frozenset[DomainStatus]] = {
    DomainStatus.PENDING: frozenset({DomainStatus.ACTIVE, DomainStatus.REVOKED}),
    DomainStatus.VERIFYING: frozenset(
        {DomainStatus.ACTIVE, DomainStatus.PENDING, DomainStatus.REVOKED}
    ),
    DomainStatus.ACTIVE: frozenset({DomainStatus.SUSPENDED, DomainStatus.REVOKED}),
    DomainStatus.SUSPENDED: frozenset({DomainStatus.ACTIVE, DomainStatus.REVOKED}),
    DomainStatus.REVOKED: frozenset(),
}


__all__ = [
    "LEGAL_TRANSITIONS",
    "CustomDomainDoc",
    "DomainStatus",
    "VerificationMethod",
]
