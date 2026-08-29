"""
Repository for the `emojis` MongoDB collection.

Identical structure to LegacyUrlRepository — same schema (EmojiUrlDoc
extends LegacyUrlDoc), same v1 update patterns. The only difference is
which MongoDB collection operations target.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pymongo.errors import DuplicateKeyError, PyMongoError, WriteError

from infrastructure.logging import get_logger
from repositories.base import BaseRepository
from schemas.models.url import EmojiUrlDoc
from shared.emoji_policy import vs16_insensitive_pattern

log = get_logger(__name__)

_DOCUMENT_TOO_LARGE_CODE = 10334


class EmojiUrlRepository(BaseRepository[EmojiUrlDoc]):
    async def find_by_id(self, alias: str) -> EmojiUrlDoc | None:
        """Find an emoji URL document by its alias (_id)."""
        return await self._find_one({"_id": alias})

    async def count_by_dest_host(self, host: str) -> int:
        """Emoji links pointing at *host* (via the stamped dest subdoc) —
        same count surface as LegacyUrlRepository's."""
        return await self._count({"dest.host": host})

    # ── Safety enforcement surface ────────────────────────────────────────
    # Mirrors LegacyUrlRepository (this class deliberately does not inherit
    # it); see there for the collect-then-flip ordering rationale.

    async def list_by_dest_host(
        self, host: str, *, limit: int = 50_000
    ) -> list[tuple[str, str]]:
        """(alias, url) of every emoji link pointing at *host*, regardless
        of blocked state — see LegacyUrlRepository.list_by_dest_host."""
        cursor = self._col.find({"dest.host": host}, {"_id": 1, "url": 1}).limit(limit)
        docs = await cursor.to_list(length=limit)
        if len(docs) >= limit:
            log.warning("dest_host_listing_truncated", host=host, limit=limit)
        return [(d["_id"], d.get("url", "")) for d in docs]

    async def unblock_by_dest_host(self, host: str) -> int:
        """Reverse a safety host block on emoji links. Stamps stay,
        ``unblocked_at`` records the reversal."""
        result = await self._col.update_many(
            {"dest.host": host, "blocked": True},
            {
                "$unset": {"blocked": ""},
                "$set": {"unblocked_at": datetime.now(timezone.utc)},
            },
        )
        return int(result.modified_count)

    async def block_by_ids(self, aliases: list[str], *, reason: str) -> int:
        """Flip specific not-yet-blocked emoji links by alias."""
        if not aliases:
            return 0
        result = await self._col.update_many(
            {"_id": {"$in": aliases}, "blocked": {"$ne": True}},
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
        """Flip every not-yet-blocked emoji link pointing at *host*."""
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

    async def unblock(self, alias: str) -> bool:
        """Reverse a safety block on one emoji link; stamps stay. The caller
        owns cache eviction (canonical VS16 key)."""
        result = await self._col.update_one(
            {"_id": alias, "blocked": True},
            {
                "$unset": {"blocked": ""},
                "$set": {"unblocked_at": datetime.now(timezone.utc)},
            },
        )
        return bool(result.modified_count)

    async def insert(self, alias: str, url_data: dict) -> None:
        """Insert a new emoji URL document with the alias as _id.

        The caller must not include ``_id`` in url_data — it is set here.
        """
        try:
            await self._col.insert_one({**url_data, "_id": alias})
        except DuplicateKeyError as exc:
            log.warning(
                "repo_insert_duplicate",
                collection=self._collection_name,
                alias=alias,
                error=str(exc),
            )
            raise
        except PyMongoError as exc:
            log.error(
                "repo_insert_failed",
                collection=self._collection_name,
                alias=alias,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    async def update(self, alias: str, update_ops: dict) -> None:
        """Apply a pre-built MongoDB update document to an emoji URL.

        If the update exceeds MongoDB's 16 MB document limit (due to
        unbounded $addToSet IP arrays), only total-clicks is incremented.
        """
        try:
            await self._col.update_one({"_id": alias}, update_ops)
        except WriteError as exc:
            if exc.code != _DOCUMENT_TOO_LARGE_CODE:
                raise
            inc = update_ops.get("$inc", {}).get("total-clicks", 1)
            try:
                await self._col.update_one(
                    {"_id": alias}, {"$inc": {"total-clicks": inc}}
                )
            except PyMongoError as retry_exc:
                log.error(
                    "repo_overflow_retry_failed",
                    collection=self._collection_name,
                    alias=alias,
                    error=str(retry_exc),
                    error_type=type(retry_exc).__name__,
                )
                raise
            log.info(
                "repo_document_overflow",
                collection=self._collection_name,
                alias=alias,
                msg="document exceeded 16 MB limit; click recorded with total-clicks only",
            )
        except PyMongoError as exc:
            log.error(
                "repo_update_failed",
                collection=self._collection_name,
                alias=alias,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    async def check_exists(self, alias: str) -> bool:
        """Return True if the emoji alias exists in the collection."""
        doc = await self._find_one_raw({"_id": alias}, {"_id": 1})
        return doc is not None

    async def check_exists_vs16_insensitive(self, canonical: str) -> bool:
        """True if any doc's ``_id`` equals *canonical* modulo optional
        ``U+FE0F`` after each codepoint.

        v1 accepted byte-variant emoji, so a legacy ``⭐️🎉`` must block a
        new canonical ``⭐🎉`` — otherwise the v2-first resolve order would
        shadow the live legacy link. Create-path-only; the unanchorable
        regex scan is acceptable on this small, frozen collection.
        """
        pattern = vs16_insensitive_pattern(canonical)
        doc = await self._find_one_raw({"_id": {"$regex": pattern}}, {"_id": 1})
        return doc is not None

    async def aggregate(self, pipeline: list[dict]) -> dict[str, Any] | None:
        """Run an aggregation pipeline and return the first result document.

        Returns None if the pipeline produces no results.
        """
        results = await self._aggregate(pipeline)
        return results[0] if results else None
