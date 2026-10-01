"""Repository for the ``tags`` collection."""

from __future__ import annotations

from bson import ObjectId
from pymongo.errors import DuplicateKeyError, PyMongoError

from infrastructure.logging import get_logger
from repositories.base import BaseRepository
from schemas.models.tag import TagDoc

log = get_logger(__name__)


_BY_CREATION = [("created_at", 1)]


class TagRepository(BaseRepository[TagDoc]):
    async def insert(self, doc: dict) -> ObjectId:
        """Insert one tag. ``DuplicateKeyError`` (the (owner, name) unique
        index) propagates so the service can answer 409."""
        return await self._insert(doc)

    async def find_by_id_and_owner(
        self, tag_id: ObjectId, owner_id: ObjectId
    ) -> TagDoc | None:
        return await self._find_one({"_id": tag_id, "owner_id": owner_id})

    async def find_by_ids_and_owner(
        self, tag_ids: list[ObjectId], owner_id: ObjectId
    ) -> list[TagDoc]:
        """The subset of *tag_ids* the owner has; foreign ids simply don't return."""
        if not tag_ids:
            return []
        return await self._find_many(
            {"_id": {"$in": tag_ids}, "owner_id": owner_id},
            sort=_BY_CREATION,
            limit=None,
        )

    async def find_by_names_and_owner(
        self, names: list[str], owner_id: ObjectId
    ) -> list[TagDoc]:
        if not names:
            return []
        return await self._find_many(
            {"owner_id": owner_id, "name": {"$in": names}},
            sort=_BY_CREATION,
            limit=None,
        )

    async def list_by_owner(self, owner_id: ObjectId) -> list[TagDoc]:
        """Every tag the owner has, oldest first (a stable order for the UI)."""
        return await self._find_many(
            {"owner_id": owner_id}, sort=_BY_CREATION, limit=None
        )

    async def count_by_owner(self, owner_id: ObjectId) -> int:
        return await self._count({"owner_id": owner_id})

    async def update_by_id_and_owner(
        self, tag_id: ObjectId, owner_id: ObjectId, set_ops: dict
    ) -> bool:
        """``$set`` on one owned tag. ``DuplicateKeyError`` propagates on a
        rename onto an existing name. The caller stamps ``updated_at``."""
        if not set_ops:
            raise ValueError("set_ops must not be empty")
        try:
            result = await self._col.update_one(
                {"_id": tag_id, "owner_id": owner_id}, {"$set": set_ops}
            )
            return result.matched_count > 0
        except DuplicateKeyError:
            raise
        except PyMongoError as exc:
            log.error(
                "repo_update_failed", collection=self._collection_name, error=str(exc)
            )
            raise

    async def delete_by_id_and_owner(
        self, tag_id: ObjectId, owner_id: ObjectId
    ) -> bool:
        try:
            result = await self._col.delete_one({"_id": tag_id, "owner_id": owner_id})
            return result.deleted_count > 0
        except PyMongoError as exc:
            log.error(
                "repo_delete_failed", collection=self._collection_name, error=str(exc)
            )
            raise

    async def delete_by_owner(self, owner_id: ObjectId) -> int:
        """Account-erasure cascade."""
        if not owner_id:
            raise ValueError("owner_id is required")
        return await self._delete_many({"owner_id": owner_id})
