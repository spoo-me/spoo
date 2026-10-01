"""TenantResolver protocol — Host header → tenant info, hot-path cached.

The middleware (PR4) calls ``resolve(host)`` on every request and stamps
``request.state.domain`` so downstream handlers can scope queries by
tenant. Returning ``None`` means "this host is not a known tenant" and
the route layer should 404.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from bson import ObjectId

from app.schemas.enums.domain_status import DomainStatus


@dataclass(frozen=True)
class TenantInfo:
    """Resolved tenant identity for a Host header."""

    domain_id: ObjectId | None  # None for the system default
    fqdn: str
    owner_id: ObjectId | None  # None for the system default (anonymous owner)
    status: DomainStatus
    is_system_default: bool
    # Per-domain routing config (PR4.5). All None on the system default and
    # on docs that haven't configured anything. Middleware reads these on
    # every custom-tenant request — populated from cache, so no extra DB hits.
    root_redirect: str | None = None
    not_found_redirect: str | None = None
    custom_robots_txt: str | None = None


class TenantResolver(Protocol):
    async def resolve(self, host: str) -> TenantInfo | None:
        """Return tenant info for a Host header, or None if unknown."""
        ...

    async def invalidate(self, host: str) -> None:
        """Drop any cached entry for *host*.

        Called by the orchestrator on every state transition so the next
        request reflects the new state immediately, instead of waiting for
        the cache TTL to expire. Must be a no-op when the cache backend
        (Redis) is unavailable.
        """
        ...
