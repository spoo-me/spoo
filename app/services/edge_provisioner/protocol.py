"""EdgeProvisioner protocol — abstracts the cert/edge-config plane.

Exists so the orchestrator can announce domain revocations without
coupling to Caddy's specific admin API. Future deployments could swap in
a Cloudflare-for-SaaS provisioner without touching service code.
"""

from __future__ import annotations

from typing import Protocol


class EdgeProvisioner(Protocol):
    async def announce_revoked(self, fqdn: str) -> bool:
        """Notify the edge that *fqdn* is no longer authorised.

        Returns ``True`` when the edge acknowledged the eviction (HTTP 2xx),
        ``False`` for any other outcome (transport error, 4xx, 5xx). The
        orchestrator persists the boolean as ``eviction_pending`` so the
        background worker can retry stale evictions later.

        Implementations MUST NOT raise — failures must be communicated via
        the bool. The on-demand TLS ask endpoint flipping to deny is the
        real authority; this call just nudges the edge to evict eagerly.
        """
        ...
