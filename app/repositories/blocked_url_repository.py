"""
Repository for the `blocked-urls` MongoDB collection.

The collection stores regex patterns used to block malicious/spam URLs.
Patterns are loaded via get_patterns() which should be called by the service
layer with appropriate caching — this repository does NOT cache results.
"""

from __future__ import annotations

from pymongo.errors import PyMongoError

from app.infrastructure.logging import get_logger
from app.repositories.base import BaseRepository

log = get_logger(__name__)


class BlockedUrlRepository(BaseRepository[None]):
    async def get_patterns(self) -> list[str]:
        """Return all blocked URL regex patterns from the collection.

        Each document's ``_id`` is the pattern string.
        """
        try:
            cursor = self._col.find({}, {"_id": 1})
            docs = await cursor.to_list(length=None)
            return [doc["_id"] for doc in docs]
        except PyMongoError as exc:
            log.error(
                "repo_get_patterns_failed",
                collection=self._collection_name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise
