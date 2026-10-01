"""
Repository for the ``blocked_domains`` MongoDB collection.

Mirrors ``BlockedUrlRepository``'s shape: ``_id`` is the canonical fqdn
(lowercased, trailing-dot stripped). The collection holds operator-curated
fqdns we refuse as custom domains (phishing-target lookalikes, abuse
hostnames, top-popular brand names from Tranco, etc.).

The service layer queries this on every create — no per-process caching
here so an operator can add an abuse domain live without restarting the
app. If the list grows large (10k+) revisit with TTL cache in the service.
"""

from __future__ import annotations

from pymongo.errors import PyMongoError

from app.infrastructure.logging import get_logger
from app.repositories.base import BaseRepository

log = get_logger(__name__)


class BlockedDomainRepository(BaseRepository[None]):
    async def list_all(self) -> set[str]:
        """Return the full set of blocked fqdns."""
        try:
            cursor = self._col.find({}, {"_id": 1})
            docs = await cursor.to_list(length=None)
            return {doc["_id"] for doc in docs}
        except PyMongoError as exc:
            log.error(
                "repo_list_all_blocked_domains_failed",
                collection=self._collection_name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    async def is_blocked(self, fqdn: str) -> bool:
        """Cheap point-lookup against the unique ``_id`` index."""
        try:
            doc = await self._col.find_one({"_id": fqdn}, {"_id": 1})
            return doc is not None
        except PyMongoError as exc:
            log.error(
                "repo_is_blocked_domain_failed",
                collection=self._collection_name,
                fqdn=fqdn,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise
