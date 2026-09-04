"""
Repository for the `api-keys` MongoDB collection.

All methods are async. Returns ApiKeyDoc models where applicable.
Errors are handled by BaseRepository.
"""

from __future__ import annotations

from datetime import datetime, timezone

from bson import ObjectId
from pymongo.errors import PyMongoError

from infrastructure.logging import get_logger
from repositories.base import BaseRepository
from schemas.models.api_key import ApiKeyDoc

log = get_logger(__name__)


class ApiKeyRepository(BaseRepository[ApiKeyDoc]):
    async def insert(self, doc: dict) -> ObjectId:
        """Insert a new API key document. Returns the inserted _id."""
        return await self._insert(doc)

    async def find_by_hash(self, token_hash: str) -> ApiKeyDoc | None:
        """Find an API key document by its SHA-256 token hash."""
        return await self._find_one({"token_hash": token_hash})

    async def list_by_user(self, user_id: ObjectId) -> list[ApiKeyDoc]:
        """Return all API keys for a user, sorted by creation time ascending."""
        try:
            cursor = self._col.find({"user_id": user_id}).sort("created_at", 1)
            docs = await cursor.to_list(length=None)
            return [ApiKeyDoc.from_mongo(d) for d in docs]  # type: ignore[misc]
        except PyMongoError as exc:
            log.error(
                "repo_list_by_user_failed",
                collection=self._collection_name,
                user_id=str(user_id),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    async def revoke(
        self, user_id: ObjectId, key_id: ObjectId, *, hard_delete: bool = False
    ) -> bool:
        """Soft-revoke or hard-delete an API key.

        Returns True if the operation affected a document (ownership check
        is enforced by matching both _id and user_id).
        """
        if hard_delete:
            return await self._delete({"_id": key_id, "user_id": user_id})
        return await self._update(
            {"_id": key_id, "user_id": user_id},
            {"$set": {"revoked": True}},
        )

    async def touch_last_used(self, key_id: ObjectId) -> None:
        """Stamp last_used_at on a key. Callers debounce and treat as best-effort."""
        await self._update(
            {"_id": key_id},
            {"$set": {"last_used_at": datetime.now(timezone.utc)}},
        )

    async def delete_by_user(self, user_id: ObjectId) -> int:
        """Hard-delete ALL of a user's API keys, revoked ones included.

        Account-erasure path — the soft ``revoked`` flag is an audit
        nicety, not something erasure honours. Returns the number deleted.
        """
        return await self._delete_many({"user_id": user_id})

    async def set_paused_by_limit(self, ids: list[ObjectId], paused: bool) -> int:
        result = await self._col.update_many(
            {"_id": {"$in": ids}}, {"$set": {"paused_by_limit": paused}}
        )
        return result.modified_count

    async def count_by_user(self, user_id: ObjectId) -> int:
        """Count active (non-revoked) API keys for a user."""
        return await self._count({"user_id": user_id, "revoked": {"$ne": True}})
