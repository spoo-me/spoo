"""DomainVerifier protocol + shared result type.

Each implementation owns one verification strategy (CNAME, A-record, TXT
challenge) and returns a uniform result so the orchestrator can drive
state transitions without caring how the proof was obtained.

Verifiers MUST NOT raise on DNS failures — every NXDOMAIN, timeout, or
mismatch becomes a ``VerificationResult(verified=False, ...)``. Raising
would crash the calling worker and block other domains.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class VerificationResult:
    """Outcome of a single verification attempt."""

    verified: bool
    # Free-form, human-readable reason. On success this can be empty; on
    # failure it surfaces back to the user via ``last_verification_error``
    # so they can debug DNS without contacting support.
    reason: str = ""


class DomainVerifier(Protocol):
    """Strategy interface for proving ownership of an fqdn."""

    async def verify(self, fqdn: str, token: str | None = None) -> VerificationResult:
        """Check whether *fqdn*'s DNS records prove ownership.

        Args:
            fqdn:  The hostname under test (already lowercased / stripped).
            token: TXT challenge token; ignored by verifiers that don't
                   need one (CNAME, A-record).
        """
        ...
