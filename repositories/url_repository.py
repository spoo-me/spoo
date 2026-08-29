"""
Repository for the `urlsV2` MongoDB collection.

All methods are async and return typed Pydantic document models.
Errors are handled by BaseRepository — domain methods delegate to
shared CRUD helpers.
"""

from __future__ import annotations

from datetime import datetime, timezone

from bson import ObjectId
from pymongo.errors import DuplicateKeyError, PyMongoError

from infrastructure.logging import get_logger
from repositories.base import BaseRepository
from schemas.models.base import ANONYMOUS_OWNER_ID
from schemas.models.url import UrlStatus, UrlV2Doc

log = get_logger(__name__)


# Mongo caps a single query document at 16MB; a host-wide block can name
# tens of thousands of (alias, domain) pairs, so the $or is chunked.
_ALIAS_CHUNK = 1_000


def _alias_chunks(
    pairs: list[tuple[str, str]],
) -> list[list[dict[str, str]]]:
    clauses = [{"alias": a, "domain": d} for a, d in pairs]
    return [clauses[i : i + _ALIAS_CHUNK] for i in range(0, len(clauses), _ALIAS_CHUNK)]


class UrlRepository(BaseRepository[UrlV2Doc]):
    async def find_by_alias(self, alias: str, domain: str) -> UrlV2Doc | None:
        """Find a URL by ``(alias, domain)``."""
        return await self._find_one({"alias": alias, "domain": domain})

    async def find_by_id(self, url_id: ObjectId) -> UrlV2Doc | None:
        """Find a URL document by its ObjectId."""
        return await self._find_one({"_id": url_id})

    async def find_by_id_and_owner(
        self, url_id: ObjectId, owner_id: ObjectId
    ) -> UrlV2Doc | None:
        """Find a URL by ObjectId, scoped to its owner.

        Ownership lives IN the query so a foreign id answers exactly like a
        missing one — read surfaces must not confirm that someone else's
        URL exists.
        """
        return await self._find_one({"_id": url_id, "owner_id": owner_id})

    async def find_by_alias_and_owner(
        self, alias: str, domain: str, owner_id: ObjectId
    ) -> UrlV2Doc | None:
        """Find a URL by ``(alias, domain)``, scoped to its owner.

        Same no-existence-oracle shape as ``find_by_id_and_owner`` — a
        foreign link is indistinguishable from a missing one.
        """
        return await self._find_one(
            {"alias": alias, "domain": domain, "owner_id": owner_id}
        )

    async def insert(self, doc: dict) -> ObjectId:
        """Insert a new URL document. Returns the inserted _id."""
        return await self._insert(doc)

    async def update(self, url_id: ObjectId, update_ops: dict) -> bool:
        """Apply a MongoDB update document to a URL.

        Returns True if the document was matched (and potentially modified).
        """
        return await self._update({"_id": url_id}, update_ops)

    async def delete(self, url_id: ObjectId) -> bool:
        """Hard-delete a URL document. Returns True if a document was deleted."""
        return await self._delete({"_id": url_id})

    async def claim_by_token_hash(
        self,
        url_id: ObjectId,
        token_hash: str,
        new_owner_id: ObjectId,
        claimed_at: datetime,
    ) -> bool:
        """CAS-transfer an anonymous URL to *new_owner_id*.

        Hash + anonymous owner live in the filter, so races and burned
        tokens match nothing. Hash unset on success — single use.
        """
        return await self._update(
            {
                "_id": url_id,
                "claim_token_hash": token_hash,
                "owner_id": ANONYMOUS_OWNER_ID,
            },
            {
                "$set": {
                    "owner_id": new_owner_id,
                    "claimed_at": claimed_at,
                    "updated_at": claimed_at,
                },
                "$unset": {"claim_token_hash": ""},
            },
        )

    async def count_claimed(self, owner_id: ObjectId) -> int:
        """How many links the owner has claimed in (``claimed_at`` present).

        Backs the per-account claim ceiling; served by the owner_claimed
        partial index.
        """
        return await self._count(
            {"owner_id": owner_id, "claimed_at": {"$exists": True}}
        )

    async def list_claimed_ids(
        self, owner_id: ObjectId, *, limit: int = 1024
    ) -> list[ObjectId]:
        """ids of the owner's claimed-in links (``claimed_at`` present).

        Served by the owner_claimed partial index (holds only claimed
        links platform-wide — sub-ms). The cap is a backstop above the
        write-side claim ceiling: unreachable by construction, so a full
        cursor means something bypassed the ceiling and scope=all stats
        are silently undercounting — exactly what the warning is for.
        """
        try:
            cursor = self._col.find(
                {"owner_id": owner_id, "claimed_at": {"$exists": True}},
                {"_id": 1},
            ).limit(limit)
            ids = [d["_id"] async for d in cursor]
        except PyMongoError as exc:
            log.error(
                "repo_list_claimed_ids_failed",
                collection=self._collection_name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise
        if len(ids) >= limit:
            log.warning(
                "claimed_ids_truncated",
                owner_id=str(owner_id),
                limit=limit,
            )
        return ids

    async def record_meta_image_validation(
        self, url_id: ObjectId, image_url: str, meta: dict
    ) -> bool:
        """CAS-write async image-validation results.

        Filtering on the CURRENT image URL means a user edit that raced the
        validator makes this a no-op instead of clobbering the new image.
        """
        return await self._update(
            {"_id": url_id, "meta_tags.image": image_url},
            {"$set": {"meta_tags.image_meta": meta}},
        )

    async def clear_meta_image(self, url_id: ObjectId, image_url: str) -> bool:
        """CAS-clear an image that failed async validation."""
        return await self._update(
            {"_id": url_id, "meta_tags.image": image_url},
            {"$set": {"meta_tags.image": None, "meta_tags.image_meta": None}},
        )

    async def list_aliases_by_owner_and_domain(
        self, owner_id: ObjectId, domain: str
    ) -> list[str]:
        """Return all aliases owned by *owner_id* under *domain*.

        Used by bulk-delete to drive cache invalidation. Two-step (list then
        delete) trades atomicity for explicit cache cleanup — a cache miss
        post-delete is correct behavior anyway.
        """
        try:
            cursor = self._col.find(
                {"owner_id": owner_id, "domain": domain},
                projection={"alias": 1, "_id": 0},
            )
            docs = await cursor.to_list(length=None)
            return [d["alias"] for d in docs if "alias" in d]
        except PyMongoError as exc:
            log.error(
                "repo_list_aliases_failed",
                collection=self._collection_name,
                error=str(exc),
            )
            raise

    async def delete_many_by_owner_and_domain(
        self, owner_id: ObjectId, domain: str
    ) -> int:
        """Bulk-delete all URLs owned by *owner_id* under *domain*.

        Both filters required defensively — a missing or empty arg here would
        silently delete more than intended.
        """
        if not owner_id or not domain:
            raise ValueError("owner_id and domain are both required for bulk delete")
        try:
            result = await self._col.delete_many(
                {"owner_id": owner_id, "domain": domain}
            )
            return int(result.deleted_count or 0)
        except PyMongoError as exc:
            log.error(
                "repo_delete_many_failed",
                collection=self._collection_name,
                error=str(exc),
            )
            raise

    async def find_by_ids_and_owner(
        self, url_ids: list[ObjectId], owner_id: ObjectId
    ) -> list[UrlV2Doc]:
        """Fetch the subset of *url_ids* owned by *owner_id*.

        The ownership-scoped batch fetch behind /urls/bulk/*: ownership is
        enforced IN the query so a foreign id simply doesn't come back —
        never as a post-fetch compare (fail closed). Both filters required
        defensively, mirroring the bulk-delete guard below.
        """
        if not url_ids or not owner_id:
            raise ValueError("url_ids and owner_id are both required for bulk fetch")
        try:
            cursor = self._col.find({"_id": {"$in": url_ids}, "owner_id": owner_id})
            docs = await cursor.to_list(length=None)
            return [UrlV2Doc.from_mongo(doc) for doc in docs]
        except PyMongoError as exc:
            log.error(
                "repo_find_by_ids_failed",
                collection=self._collection_name,
                error=str(exc),
            )
            raise

    async def delete_by_ids_and_owner(
        self, url_ids: list[ObjectId], owner_id: ObjectId
    ) -> int:
        """Bulk-delete exactly *url_ids* if owned by *owner_id*.

        Both filters required defensively — the compound filter keeps the
        write fail-closed even if a caller's pre-fetch went stale.
        """
        if not url_ids or not owner_id:
            raise ValueError("url_ids and owner_id are both required for bulk delete")
        try:
            result = await self._col.delete_many(
                {"_id": {"$in": url_ids}, "owner_id": owner_id}
            )
            return int(result.deleted_count or 0)
        except PyMongoError as exc:
            # Distinct from the domain-cascade's repo_delete_many_failed:
            # a targeted by-ids delete failing is a different triage story
            # than a domain-wide wipe failing.
            log.error(
                "repo_delete_by_ids_failed",
                collection=self._collection_name,
                error=str(exc),
            )
            raise

    async def update_by_ids_and_owner(
        self, url_ids: list[ObjectId], owner_id: ObjectId, set_ops: dict
    ) -> int:
        """Apply one ``$set`` to exactly *url_ids* if owned by *owner_id*.

        Same defensive posture as the bulk delete above; *set_ops* is the
        bare field map (the ``$set`` wrapper is applied here).
        """
        if not url_ids or not owner_id:
            raise ValueError("url_ids and owner_id are both required for bulk update")
        if not set_ops:
            raise ValueError("set_ops must not be empty")
        try:
            result = await self._col.update_many(
                {"_id": {"$in": url_ids}, "owner_id": owner_id}, {"$set": set_ops}
            )
            return int(result.modified_count or 0)
        except DuplicateKeyError:
            # Expected outcome, not a failure: the (domain, alias) unique
            # index arbitrates alias races on domain moves and the caller
            # maps the violation to a per-item conflict. Logging this at
            # ERROR would page people for working-as-designed behavior.
            raise
        except PyMongoError as exc:
            log.error(
                "repo_update_many_failed",
                collection=self._collection_name,
                error=str(exc),
            )
            raise

    async def check_alias_exists(self, alias: str, domain: str) -> bool:
        """Return True if the alias is taken under the given domain namespace."""
        doc = await self._find_one_raw({"alias": alias, "domain": domain}, {"_id": 1})
        return doc is not None

    # ── Safety enforcement surface ────────────────────────────────────────
    # Equality matches on dest.host (sparse index). Docs predating the backfill
    # don't match — the backfill runs before enforcement is enabled.

    async def list_by_dest_host_with_urls(
        self, host: str, *, limit: int = 50_000
    ) -> list[tuple[str, str, str]]:
        """(alias, domain, long_url) of every link pointing at *host*,
        deliberately status-blind: a re-delivered block must still evict
        entries the first attempt flipped but failed to evict."""
        cursor = self._col.find(
            {"dest.host": host},
            {"alias": 1, "domain": 1, "long_url": 1},
        ).limit(limit)
        docs = await cursor.to_list(length=limit)
        if len(docs) >= limit:
            log.warning(
                "dest_host_listing_truncated",
                collection=self._collection_name,
                host=host,
                limit=limit,
            )
        return [(d["alias"], d.get("domain", ""), d.get("long_url", "")) for d in docs]

    async def unblock_by_dest_host(self, host: str) -> int:
        """Flip BLOCKED links pointing at *host* back to ACTIVE, scoped to
        docs carrying ``blocked_reason`` so a manual operator ban is never
        undone. Stamps stay; ``unblocked_at`` records the reversal."""
        now = datetime.now(timezone.utc)
        result = await self._col.update_many(
            {
                "dest.host": host,
                "status": UrlStatus.BLOCKED.value,
                "blocked_reason": {"$exists": True},
            },
            {
                "$set": {
                    "status": UrlStatus.ACTIVE.value,
                    "unblocked_at": now,
                    "updated_at": now,
                }
            },
        )
        return int(result.modified_count)

    async def list_active_owned_by_dest_host(
        self, host: str, *, limit: int = 1_000
    ) -> list[UrlV2Doc]:
        """Full docs for OWNED active links to *host* — the link.blocked
        event set (anonymous links have no possible webhook subscriber)."""
        cursor = self._col.find(
            {
                "dest.host": host,
                "status": UrlStatus.ACTIVE.value,
                "owner_id": {"$ne": ANONYMOUS_OWNER_ID},
            }
        ).limit(limit)
        docs = await cursor.to_list(length=limit)
        if len(docs) >= limit:
            # Owners past the cap get no link.blocked event at all.
            log.warning("owned_event_set_truncated", host=host, limit=limit)
        return [UrlV2Doc.from_mongo(d) for d in docs]

    async def list_active_hosts_by_registrable(
        self, registrable_domain: str, *, limit: int = 200
    ) -> list[tuple[str, str]]:
        """Distinct ACTIVE destination hosts under one registrable domain,
        each with a sample long_url — the feed-delta sweep's fan-out unit
        (verdicts are host-keyed, feeds are domain-keyed)."""
        pipeline = [
            {
                "$match": {
                    "dest.registrable_domain": registrable_domain,
                    "status": UrlStatus.ACTIVE.value,
                }
            },
            {
                "$group": {
                    "_id": "$dest.host",
                    "sample_url": {"$first": "$long_url"},
                }
            },
            {"$limit": limit},
        ]
        docs = await self._aggregate(pipeline)
        if len(docs) >= limit:
            # Loud truncation: silence would read as full coverage.
            log.warning(
                "feed_delta_hosts_truncated",
                registrable_domain=registrable_domain,
                limit=limit,
            )
        return [(d["_id"], d.get("sample_url", "")) for d in docs if d.get("_id")]

    async def list_recent_destination_hosts(
        self, since: datetime, *, limit: int = 20_000
    ) -> list[tuple[str, str]]:
        """Distinct destination hosts of links created since *since*, each
        with a sample long_url. Rides the _id index (ObjectIds embed their
        creation time) — no extra index needed for the screening sweep."""
        since_id = ObjectId.from_datetime(since)
        pipeline = [
            {"$match": {"_id": {"$gte": since_id}, "dest.host": {"$exists": True}}},
            {
                "$group": {
                    "_id": "$dest.host",
                    "sample_url": {"$first": "$long_url"},
                }
            },
            {"$limit": limit},
        ]
        docs = await self._aggregate(pipeline)
        return [(d["_id"], d.get("sample_url", "")) for d in docs if d.get("_id")]

    async def list_active_owned_by_aliases(
        self, pairs: list[tuple[str, str]], *, limit: int = 1_000
    ) -> list[UrlV2Doc]:
        """Full docs for OWNED active links among *(alias, domain)* pairs —
        the link.blocked event set for a per-link block."""
        if not pairs:
            return []
        docs: list[dict] = []
        for chunk in _alias_chunks(pairs):
            if len(docs) >= limit:
                break
            cursor = self._col.find(
                {
                    "$or": chunk,
                    "status": UrlStatus.ACTIVE.value,
                    "owner_id": {"$ne": ANONYMOUS_OWNER_ID},
                }
            ).limit(limit - len(docs))
            docs.extend(await cursor.to_list(length=limit - len(docs)))
        return [UrlV2Doc.from_mongo(d) for d in docs]

    async def block_active_by_aliases(
        self, pairs: list[tuple[str, str]], *, reason: str
    ) -> int:
        """Flip specific ACTIVE links to BLOCKED by (alias, domain). The
        per-link enforcement path: a compromised legitimate site or a
        redirector endpoint gets its aliases blocked without a host-wide
        verdict — which makes the doc-level ``blocked_reason`` the ONLY
        reason-of-record for these. Idempotent like the host-wide flip."""
        if not pairs:
            return 0
        now = datetime.now(timezone.utc)
        modified = 0
        for chunk in _alias_chunks(pairs):
            result = await self._col.update_many(
                {"$or": chunk, "status": UrlStatus.ACTIVE.value},
                {
                    "$set": {
                        "status": UrlStatus.BLOCKED.value,
                        "updated_at": now,
                        "blocked_at": now,
                        "blocked_reason": reason,
                    }
                },
            )
            modified += int(result.modified_count)
        return modified

    async def destination_history(self, host: str) -> dict:
        """First-party history of a destination host — the deep tier's
        strongest FREE signal. One aggregation over the dest.host index:
        how many links point here, the anon/owned split, total clicks, and
        the earliest sighting. All facts we already own; no network."""
        pipeline = [
            {"$match": {"dest.host": host}},
            {
                "$group": {
                    "_id": None,
                    "link_count": {"$sum": 1},
                    "anon_count": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$owner_id", ANONYMOUS_OWNER_ID]},
                                1,
                                0,
                            ]
                        }
                    },
                    "total_clicks": {"$sum": {"$ifNull": ["$total_clicks", 0]}},
                    "distinct_owners": {"$addToSet": "$owner_id"},
                    "first_seen": {"$min": "$_id"},
                    "edited_count": {
                        "$sum": {"$cond": [{"$ifNull": ["$updated_at", False]}, 1, 0]}
                    },
                }
            },
        ]
        docs = await self._aggregate(pipeline)
        if not docs:
            return {
                "link_count": 0,
                "anon_count": 0,
                "owned_count": 0,
                "distinct_owners": 0,
                "total_clicks": 0,
                "first_seen": None,
                "edited_count": 0,
            }
        d = docs[0]
        owners = [o for o in d.get("distinct_owners", []) if o != ANONYMOUS_OWNER_ID]
        first = d.get("first_seen")
        return {
            "link_count": d.get("link_count", 0),
            "anon_count": d.get("anon_count", 0),
            "owned_count": d.get("link_count", 0) - d.get("anon_count", 0),
            "distinct_owners": len(owners),
            "total_clicks": d.get("total_clicks", 0),
            "first_seen": (
                first.generation_time.isoformat() if first is not None else None
            ),
            "edited_count": d.get("edited_count", 0),
        }

    async def host_breadth(self, host: str, *, sample: int = 8) -> dict:
        """How WIDELY a destination host is used on the platform — the
        evidence for deciding whether abuse is the host or one path on it.

        A host with hundreds of links across many distinct paths and many
        distinct creators is a shared platform (Google Sites, raw
        githubusercontent, a website builder): blocking it host-wide
        punishes every unrelated tenant. A host whose every link is the
        same handful of paths from one anonymous creator is a purpose-built
        destination. Also reports how many of its links are ALREADY blocked
        — a host with a long history of blocked links has earned less
        benefit of the doubt.
        """
        pipeline = [
            {"$match": {"dest.host": host}},
            {
                "$group": {
                    "_id": None,
                    "total": {"$sum": 1},
                    "blocked": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$status", UrlStatus.BLOCKED.value]},
                                1,
                                0,
                            ]
                        }
                    },
                    "paths": {"$addToSet": "$long_url"},
                    "creators": {"$addToSet": "$owner_id"},
                    "aliases": {"$addToSet": "$alias"},
                }
            },
        ]
        docs = await self._aggregate(pipeline)
        if not docs:
            return {
                "total_links": 0,
                "blocked_links": 0,
                "distinct_urls": 0,
                "distinct_creators": 0,
                "sample_urls": [],
                "sample_aliases": [],
            }
        d = docs[0]
        urls = d.get("paths", []) or []
        aliases = [a for a in (d.get("aliases", []) or []) if a]
        return {
            "total_links": d.get("total", 0),
            "blocked_links": d.get("blocked", 0),
            "distinct_urls": len(urls),
            "distinct_creators": len(d.get("creators", []) or []),
            "sample_urls": urls[:sample],
            # Personal-name aliases on throwaway pages: the identity-abuse signature.
            "sample_aliases": aliases[:sample],
        }

    async def block_active_by_dest_host(self, host: str, *, reason: str) -> int:
        """Flip every ACTIVE link pointing at *host* to BLOCKED. Returns the
        number of links flipped. Idempotent: already-BLOCKED links no longer
        match the filter. ``blocked_at``/``blocked_reason`` are the per-link
        audit trail — ``updated_at`` is lossy, these survive later edits."""
        now = datetime.now(timezone.utc)
        result = await self._col.update_many(
            {"dest.host": host, "status": UrlStatus.ACTIVE.value},
            {
                "$set": {
                    "status": UrlStatus.BLOCKED.value,
                    "updated_at": now,
                    "blocked_at": now,
                    "blocked_reason": reason,
                }
            },
        )
        return int(result.modified_count)

    async def increment_clicks(
        self,
        url_id: ObjectId,
        last_click_time: datetime | None = None,
        increment: int = 1,
    ) -> None:
        """Atomically increment total_clicks and update last_click timestamp."""
        click_time = last_click_time or datetime.now(timezone.utc)
        await self._update(
            {"_id": url_id},
            {
                "$inc": {"total_clicks": increment},
                "$set": {"last_click": click_time},
            },
        )

    async def expire_if_max_clicks(self, url_id: ObjectId, max_clicks: int) -> bool:
        """Conditionally expire the URL if total_clicks >= max_clicks.

        This is an atomic conditional update — not a read-then-write.
        Returns True only if the URL was actually expired (status changed),
        not if it was already EXPIRED. Uses ``modified_count`` to avoid
        duplicate expiration side-effects.
        """
        return await self._update_modified(
            {"_id": url_id, "total_clicks": {"$gte": max_clicks}},
            {"$set": {"status": UrlStatus.EXPIRED}},
        )

    async def expire_if_time_reached(self, url_id: ObjectId) -> bool:
        """Conditionally expire the URL if its expire_after has passed.

        Atomic conditional update mirroring ``expire_if_max_clicks`` —
        matches only ACTIVE docs so BLOCKED/INACTIVE are never clobbered.
        Returns True only if this call performed the flip (modified_count).
        ``$lte`` on a date never matches null/missing (BSON type
        bracketing), so no explicit null guard is needed.
        """
        return await self._update_modified(
            {
                "_id": url_id,
                "status": UrlStatus.ACTIVE,
                "expire_after": {"$lte": datetime.now(timezone.utc)},
            },
            {"$set": {"status": UrlStatus.EXPIRED}},
        )

    async def find_by_owner(
        self,
        query: dict,
        sort_field: str,
        sort_order: int,
        skip: int,
        limit: int,
    ) -> list[UrlV2Doc]:
        """Return a page of UrlV2Doc models matching *query*.

        The query must already include the owner_id filter (built by the
        service layer).
        """
        try:
            cursor = (
                self._col.find(query)
                .sort(sort_field, sort_order)
                .skip(skip)
                .limit(limit)
            )
            docs = await cursor.to_list(length=limit)
            return [UrlV2Doc.from_mongo(d) for d in docs]  # type: ignore[misc]
        except PyMongoError as exc:
            log.error(
                "repo_find_by_owner_failed",
                collection=self._collection_name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    async def count_by_query(self, query: dict) -> int:
        """Count documents matching query."""
        return await self._count(query)
