"""
Custom-domain lifecycle states.

A ``CustomDomainDoc`` moves through these states via the ``CustomDomainService``
state machine. Transitions are not free-form — see the service for the legal
edges. ``SYSTEM`` is the verification method reserved for the auto-seeded
system-default row; user-registered domains pick one of the three real
verification strategies.
"""

from __future__ import annotations

from enum import Enum


class DomainStatus(str, Enum):
    """Lifecycle state of a custom domain registration."""

    PENDING = "pending"  # registered, awaiting verification
    VERIFYING = "verifying"  # verification in progress (DNS check pending)
    ACTIVE = "active"  # verified; Caddy may issue a cert
    SUSPENDED = "suspended"  # re-verify failures or admin action; no new certs
    REVOKED = "revoked"  # owner deleted or admin force-removed


class VerificationMethod(str, Enum):
    """How ownership of the fqdn is proven before activation."""

    CNAME = "cname"  # CNAME <fqdn> -> custom.spoo.me
    A_RECORD = "a_record"  # A <fqdn> -> origin IPv4 (apex domains)
    TXT_CHALLENGE = "txt_challenge"  # TXT _spoo-challenge.<fqdn> = token
    SYSTEM = "system"  # reserved for the system-default row
    # CF SaaS paths — verifier polls Cloudflare instead of running DNS itself.
    # CF auto-renews certs forever once Delegated DCV CNAME is in place.
    CF_DELEGATED_DCV = "cf_delegated_dcv"
    CF_HTTP_DCV = "cf_http_dcv"
