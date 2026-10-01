"""
Repository for the `feature_flags` MongoDB collection.

Read-mostly. Mutations are rare and happen via direct mongosh edits during
rollouts (PR0 ships without an admin API). ``upsert`` and ``list_all``
exist for tests and any future bootstrap script that wants to register
known flags programmatically.
"""

from __future__ import annotations

from datetime import datetime, timezone

from bson import ObjectId
from pymongo.errors import PyMongoError

from app.infrastructure.logging import get_logger
from app.repositories.base import BaseRepository
from app.schemas.models.feature_flag import FeatureFlagDoc

log = get_logger(__name__)


class FeatureFlagRepository(BaseRepository[FeatureFlagDoc]):
    async def find_by_name(self, name: str) -> FeatureFlagDoc | None:
        """Return the flag doc by name, or None if not registered."""
        return await self._find_one({"name": name})

    async def upsert(self, name: str, fields: dict) -> ObjectId:
        """Insert or update a flag doc by name. Returns the doc's _id."""
        now = datetime.now(timezone.utc)
        # Strip keys that live in $setOnInsert — same key in both $set and
        # $setOnInsert raises a path-conflict error on MongoDB >= 4.4.
        set_fields = {
            k: v for k, v in fields.items() if k not in ("name", "created_at")
        }
        set_fields["updated_at"] = now
        result = await self._col.update_one(
            {"name": name},
            {
                "$set": set_fields,
                "$setOnInsert": {"created_at": now, "name": name},
            },
            upsert=True,
        )
        if result.upserted_id is not None:
            return result.upserted_id
        # Existing doc — fetch its _id for the return contract.
        doc = await self._find_one_raw({"name": name}, {"_id": 1})
        if doc is None:  # pragma: no cover — race only possible if doc deleted mid-call
            raise RuntimeError(
                f"feature flag {name!r} vanished between upsert and read"
            )
        return doc["_id"]

    async def pull_allowlisted(self, user_id: ObjectId, email: str) -> int:
        """``$pull`` the user's id and email out of every flag allowlist.

        Account erasure. Emails are stored lowercased by convention but
        rollout edits happen via raw mongosh, so both casings are pulled.
        Returns the number of flag documents modified.
        """
        try:
            result = await self._col.update_many(
                {},
                {
                    "$pull": {
                        "allowlist_user_ids": user_id,
                        "allowlist_emails": {"$in": [email, email.lower()]},
                    }
                },
            )
            return result.modified_count
        except PyMongoError as exc:
            log.error(
                "repo_pull_allowlisted_failed",
                collection=self._collection_name,
                user_id=str(user_id),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    async def list_all(self) -> list[FeatureFlagDoc]:
        """Return all registered flags. Used by admin scripts + tests."""
        cursor = self._col.find({})
        docs = await cursor.to_list(length=None)
        return [
            FeatureFlagDoc.from_mongo(d)  # type: ignore[misc]
            for d in docs
        ]
