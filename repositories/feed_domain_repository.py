"""Repository for the ``safety_feed_domains`` collection.

One doc per (feed, domain): ``_id`` is ``"<feed>:<domain>"`` so membership
checks are primary-key point reads — the analysis-time lookup an external
threat feed exists to answer. Mongo (the one mandatory dependency) holds
the sets, so self-hosters without Redis get feeds too and an eviction can
never silently empty a blocklist.

Refresh is a full swap per feed: upsert everything from the fresh download
with a new ``synced_at``, then purge docs still carrying an older stamp —
delisted domains disappear on the same sync that added new ones, with no
staging collection and no window where the feed reads empty.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

from pymongo import UpdateOne

from repositories.base import BaseRepository

_BULK_BATCH = 5_000


class FeedDomainRepository(BaseRepository[None]):
    @staticmethod
    def _key(feed: str, domain: str) -> str:
        return f"{feed}:{domain}"

    async def replace_feed(
        self, feed: str, domains: Iterable[str]
    ) -> tuple[int, int, set[str]]:
        """Swap *feed*'s set to exactly *domains*.

        Returns (kept, purged, new_domains) — the third element is the
        domains that were NOT in the feed before this sync, which is the
        feed-delta sweep's input: existing links pointing at a domain the
        world just listed."""
        # distinct() returns one BSON array capped at 16MB — fine for fishfish
        # (~43k domains); a URLhaus-size feed needs a projected find cursor.
        existing = set(await self._col.distinct("domain", {"feed": feed}))
        now = datetime.now(timezone.utc)
        kept = 0
        new_domains: set[str] = set()
        batch: list[UpdateOne] = []
        for raw in domains:
            domain = str(raw).strip().lower().rstrip(".")
            if not domain:
                continue
            kept += 1
            if domain not in existing:
                new_domains.add(domain)
            batch.append(
                UpdateOne(
                    {"_id": self._key(feed, domain)},
                    {"$set": {"feed": feed, "domain": domain, "synced_at": now}},
                    upsert=True,
                )
            )
            if len(batch) >= _BULK_BATCH:
                await self._col.bulk_write(batch, ordered=False)
                batch = []
        if batch:
            await self._col.bulk_write(batch, ordered=False)
        purged = 0
        if kept:
            # Only purge after a non-empty sync: an upstream outage that
            # returns [] must never wipe the last known-good set.
            result = await self._col.delete_many(
                {"feed": feed, "synced_at": {"$lt": now}}
            )
            purged = int(result.deleted_count)
        return kept, purged, new_domains

    async def contains(self, feed: str, domain: str) -> bool:
        doc = await self._col.find_one(
            {"_id": self._key(feed, domain.lower().rstrip("."))}, {"_id": 1}
        )
        return doc is not None

    async def count(self, feed: str) -> int:
        return await self._count({"feed": feed})
