"""
Repository for the `page-layouts` MongoDB collection.

One document per (user_id, page), enforced by a compound unique index.
Ownership is enforced by matching user_id in every query.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from bson import ObjectId
from pymongo.errors import PyMongoError

from app.infrastructure.logging import get_logger
from app.repositories.base import BaseRepository
from app.schemas.models.page_layout import PageLayoutDoc

log = get_logger(__name__)


class PageLayoutRepository(BaseRepository[PageLayoutDoc]):
    async def get(self, user_id: ObjectId, page: str) -> PageLayoutDoc | None:
        """Return the saved layout doc for (user, page), if any."""
        return await self._find_one({"user_id": user_id, "page": page})

    async def upsert(
        self, user_id: ObjectId, page: str, layout: dict[str, Any]
    ) -> None:
        """Create or replace the layout for (user, page)."""
        try:
            await self._col.update_one(
                {"user_id": user_id, "page": page},
                {"$set": {"layout": layout, "updated_at": datetime.now(timezone.utc)}},
                upsert=True,
            )
        except PyMongoError as exc:
            log.error(
                "repo_upsert_failed",
                collection=self._collection_name,
                user_id=str(user_id),
                page=page,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    async def delete(self, user_id: ObjectId, page: str) -> bool:
        """Remove the layout override; True if a document was deleted."""
        return await self._delete({"user_id": user_id, "page": page})

    async def delete_by_user(self, user_id: ObjectId) -> int:
        """Delete every saved layout for the user (account erasure).

        Returns the number of documents deleted.
        """
        return await self._delete_many({"user_id": user_id})
