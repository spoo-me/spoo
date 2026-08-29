"""
Repository for the `urls` collection (v1 legacy schema).

Key differences from the v2 UrlRepository:
- _id IS the short code string (not an ObjectId).
- Field names use hyphens: "max-clicks", "total-clicks", etc.
- Passwords are stored in plaintext (backward compatibility).
- Analytics are embedded on the URL document (not in a separate collection).
- The update() method has overflow-retry logic for the 16 MB document limit.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pymongo.errors import DuplicateKeyError, PyMongoError, WriteError

from infrastructure.logging import get_logger
from repositories.base import BaseRepository
from schemas.models.url import LegacyUrlDoc

log = get_logger(__name__)

_DOCUMENT_TOO_LARGE_CODE = 10334


class LegacyUrlRepository(BaseRepository[LegacyUrlDoc]):
    async def find_by_id(self, short_code: str) -> LegacyUrlDoc | None:
        """Find a v1 URL document by its short code (_id)."""
        return await self._find_one({"_id": short_code})

    async def insert(self, short_code: str, url_data: dict) -> None:
        """Insert a new v1 URL document with the short code as _id.

        The caller must not include ``_id`` in url_data — it is set here.
        """
        try:
            await self._col.insert_one({**url_data, "_id": short_code})
        except DuplicateKeyError as exc:
            log.warning(
                "repo_insert_duplicate",
                collection=self._collection_name,
                short_code=short_code,
                error=str(exc),
            )
            raise
        except PyMongoError as exc:
            log.error(
                "repo_insert_failed",
                collection=self._collection_name,
                short_code=short_code,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    async def update(self, short_code: str, update_ops: dict) -> None:
        """Apply a pre-built MongoDB update document to a v1 URL.

        If the update exceeds MongoDB's 16 MB document limit (due to
        unbounded $addToSet IP arrays), only total-clicks is incremented.
        """
        try:
            await self._col.update_one({"_id": short_code}, update_ops)
        except WriteError as exc:
            if exc.code != _DOCUMENT_TOO_LARGE_CODE:
                raise
            # $inc on an existing integer never changes BSON size.
            inc = update_ops.get("$inc", {}).get("total-clicks", 1)
            try:
                await self._col.update_one(
                    {"_id": short_code}, {"$inc": {"total-clicks": inc}}
                )
            except PyMongoError as retry_exc:
                log.error(
                    "repo_overflow_retry_failed",
                    collection=self._collection_name,
                    short_code=short_code,
                    error=str(retry_exc),
                    error_type=type(retry_exc).__name__,
                )
                raise
            log.info(
                "repo_document_overflow",
                collection=self._collection_name,
                short_code=short_code,
                msg="document exceeded 16 MB limit; click recorded with total-clicks only",
            )
        except PyMongoError as exc:
            log.error(
                "repo_update_failed",
                collection=self._collection_name,
                short_code=short_code,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    async def check_exists(self, short_code: str) -> bool:
        """Return True if the short code exists in the collection."""
        doc = await self._find_one_raw({"_id": short_code}, {"_id": 1})
        return doc is not None

    async def count_by_dest_host(self, host: str) -> int:
        """v1 links pointing at *host* (via the stamped dest subdoc)."""
        return await self._count({"dest.host": host})

    # ── Safety enforcement surface ────────────────────────────────────────
    # v1 has no status machine; enforcement is the single ``blocked`` flag
    # (absent = active). Same collect-then-flip order as v2: the id list is
    # the cache-invalidation set and must be read before the flip removes
    # docs from the not-yet-blocked filter.

    async def list_by_dest_host(
        self, host: str, *, limit: int = 50_000
    ) -> list[tuple[str, str]]:
        """(short_code, url) of every link pointing at *host*, regardless
        of blocked state — enforcement candidates and the eviction set (a
        re-delivered block must still evict already-flipped entries)."""
        cursor = self._col.find({"dest.host": host}, {"_id": 1, "url": 1}).limit(limit)
        docs = await cursor.to_list(length=limit)
        if len(docs) >= limit:
            log.warning("dest_host_listing_truncated", host=host, limit=limit)
        return [(d["_id"], d.get("url", "")) for d in docs]

    async def unblock_by_dest_host(self, host: str) -> int:
        """Reverse a safety host block on v1 links. Stamps stay,
        ``unblocked_at`` records the reversal."""
        result = await self._col.update_many(
            {"dest.host": host, "blocked": True},
            {
                "$unset": {"blocked": ""},
                "$set": {"unblocked_at": datetime.now(timezone.utc)},
            },
        )
        return int(result.modified_count)

    async def block_by_ids(self, short_codes: list[str], *, reason: str) -> int:
        """Flip specific not-yet-blocked links by short code."""
        if not short_codes:
            return 0
        result = await self._col.update_many(
            {"_id": {"$in": short_codes}, "blocked": {"$ne": True}},
            {
                "$set": {
                    "blocked": True,
                    "blocked_at": datetime.now(timezone.utc),
                    "blocked_reason": reason,
                }
            },
        )
        return int(result.modified_count)

    async def block_by_dest_host(self, host: str, *, reason: str) -> int:
        """Flip every not-yet-blocked link pointing at *host*. Returns the
        number flipped; idempotent like the v2 status flip. ``blocked_at``
        and ``blocked_reason`` are the per-link audit trail — ``$ne`` in
        the filter means a re-block never overwrites the original stamp."""
        result = await self._col.update_many(
            {"dest.host": host, "blocked": {"$ne": True}},
            {
                "$set": {
                    "blocked": True,
                    "blocked_at": datetime.now(timezone.utc),
                    "blocked_reason": reason,
                }
            },
        )
        return int(result.modified_count)

    async def unblock(self, short_code: str) -> bool:
        """Reverse a safety block — the thing deletion could never offer.
        Only the flag goes; ``blocked_at``/``blocked_reason`` stay and
        ``unblocked_at`` is stamped, so a wrong block that was quietly
        reversed still shows what happened and when. The wire shape is a
        DTO concern — resolution only reads the flag.

        The caller owns cache eviction (same contract as bulk-delete):
        without it the cached BLOCKED entry keeps serving 451s until TTL."""
        result = await self._col.update_one(
            {"_id": short_code, "blocked": True},
            {
                "$unset": {"blocked": ""},
                "$set": {"unblocked_at": datetime.now(timezone.utc)},
            },
        )
        return bool(result.modified_count)

    async def aggregate(self, pipeline: list[dict]) -> dict[str, Any] | None:
        """Run an aggregation pipeline and return the first result document.

        Returns None if the pipeline produces no results.
        """
        results = await self._aggregate(pipeline)
        return results[0] if results else None
