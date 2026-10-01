"""Bulk URL operations (``POST /api/v1/urls/bulk/*``).

Set-based twin of ``UrlService``'s single-item mutations: one
ownership-scoped fetch answers every per-item verdict, one set-write
mutates the actionable slice, side effects run grouped (Redis per
domain, at most one detached Cloudflare KV call-pair per batch), and
every unique requested id gets exactly one row in the result report.

Semantics MUST mirror the single-item paths item-for-item —
``UrlService.delete`` and the ``status``/``expire_after`` field
handlers in :mod:`services.url_service` are the contract, and the
parity suite in ``tests/unit/services/test_bulk_url_service.py`` pins
the equivalence. Change either side only with both open. The one
deliberate divergence: ownership lives IN the fetch query, so someone
else's id and a nonexistent id both answer ``not_found`` (no existence
oracle over foreign ObjectIds).

Kept per-op rather than abstracted: the three writes are structurally
different (one delete, one update, two updates), so the shared parts
are the stage helpers below, not a template method. If the op set ever
grows past ~6 shape-uniform ops, promote the stages into a spec-driven
engine — composition, not inheritance.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from bson import ObjectId
from pymongo.errors import DuplicateKeyError, PyMongoError

from app.errors import ValidationError
from app.infrastructure.cache.url_cache import UrlCacheData
from app.infrastructure.logging import get_logger
from app.schemas.dto.responses.bulk import (
    BulkOperationSummary,
    BulkUrlOperationResponse,
    BulkUrlResultRow,
)
from app.schemas.models.url import UrlStatus, UrlV2Doc
from app.services.edge_cache.contract import cache_key
from app.services.edge_cache.og_writethrough import build_og_entry
from app.services.events.contract import DomainEvent
from app.services.events.sinks import NullDomainEventSink
from app.services.webhooks.payloads import event_changes, link_owner_id, link_snapshot
from app.shared.reserved_aliases import is_reserved_alias
from app.shared.tags import TAGS_MAX_PER_LINK

if TYPE_CHECKING:
    from app.infrastructure.cache.url_cache import UrlCache
    from app.infrastructure.cloudflare_kv import CloudflareKVClient
    from app.repositories.url_repository import UrlRepository
    from app.services.events.protocol import DomainEventSink
    from app.services.tag_service import TagService
    from app.services.url_service import UrlService

log = get_logger(__name__)

_INTERNAL_MSG = "Unexpected failure; retry these items"


class BulkBatch:
    """One verdict per unique requested id.

    ``report()`` raises if any id lacks a verdict — "no item is ever
    silently skipped" enforced structurally rather than by review. The
    summary is always derived from the rows, never counted separately.
    """

    def __init__(self, ids: list[ObjectId], docs: dict[ObjectId, UrlV2Doc]) -> None:
        self.order = ids
        self.docs = docs
        self._rows: dict[ObjectId, BulkUrlResultRow] = {}

    def reject(
        self, url_id: ObjectId, code: str, message: str, *, alias: str | None = None
    ) -> None:
        self._rows[url_id] = BulkUrlResultRow(
            id=str(url_id), alias=alias, ok=False, error_code=code, error=message
        )

    def ok(self, url_id: ObjectId, *, alias: str) -> None:
        self._rows[url_id] = BulkUrlResultRow(id=str(url_id), alias=alias, ok=True)

    @property
    def pending(self) -> list[UrlV2Doc]:
        """Fetched docs without a verdict yet — the actionable set."""
        return [
            self.docs[i] for i in self.order if i in self.docs and i not in self._rows
        ]

    def report(self, *, op: str, user_id: ObjectId) -> BulkUrlOperationResponse:
        missing = [i for i in self.order if i not in self._rows]
        if missing:
            raise RuntimeError(
                f"bulk {op} report is missing verdicts for {len(missing)} ids"
            )
        rows = [self._rows[i] for i in self.order]
        succeeded = sum(1 for row in rows if row.ok)
        histogram: dict[str, int] = {}
        for row in rows:
            if not row.ok and row.error_code:
                histogram[row.error_code] = histogram.get(row.error_code, 0) + 1
        # The error-code histogram is the abuse signal: a key producing
        # high-not_found batches is enumerating ids.
        log.info(
            "urls_bulk_op",
            op=op,
            user_id=str(user_id),
            total=len(rows),
            succeeded=succeeded,
            failed=len(rows) - succeeded,
            error_codes=histogram,
        )
        return BulkUrlOperationResponse(
            summary=BulkOperationSummary(
                total=len(rows), succeeded=succeeded, failed=len(rows) - succeeded
            ),
            results=rows,
        )


class BulkUrlService:
    """Set-based bulk mutations over URLs the caller owns."""

    def __init__(
        self,
        url_repo: UrlRepository,
        url_cache: UrlCache,
        *,
        url_service: UrlService,
        kv: CloudflareKVClient | None,
        system_default_domain: str,
        og_ttl_seconds: int = 86_400,
        events: DomainEventSink | None = None,
        tag_service: TagService | None = None,
    ) -> None:
        self._url_repo = url_repo
        self._tag_service = tag_service
        self._url_cache = url_cache
        # Business rules that must never fork are borrowed, not re-derived:
        # alias availability (v2 + legacy + emoji on the system default)
        # comes from the same method the single-item domain handler calls.
        self._url_service = url_service
        self._kv = kv
        self._system_default_domain = system_default_domain
        self._og_ttl_seconds = og_ttl_seconds
        # Domain-event sink (webhooks backbone). Per-item emission: a bulk
        # op is N facts, and subscribers must not be able to tell bulk
        # from a loop — same contract _log_updated already keeps for logs.
        self._events = events or NullDomainEventSink()
        self._inflight: set[asyncio.Task] = set()

    # ── shared stages ────────────────────────────────────────────────────

    async def _load(
        self, ids: list[ObjectId], owner_id: ObjectId, *, blocked_message: str
    ) -> BulkBatch:
        """Dedupe, fetch ownership-scoped, hand out the base verdicts.

        Foreign and nonexistent ids are indistinguishable by design (the
        fetch is the ownership check); BLOCKED links fail per-item with
        the same message the single-item guard raises.
        """
        seen: set[ObjectId] = set()
        unique: list[ObjectId] = []
        for url_id in ids:
            if url_id not in seen:
                seen.add(url_id)
                unique.append(url_id)

        docs = await self._url_repo.find_by_ids_and_owner(unique, owner_id)
        by_id = {doc.id: doc for doc in docs}
        batch = BulkBatch(unique, by_id)
        for url_id in unique:
            doc = by_id.get(url_id)
            if doc is None:
                batch.reject(url_id, "not_found", "URL not found")
            elif doc.status == UrlStatus.BLOCKED:
                batch.reject(url_id, "forbidden", blocked_message, alias=doc.alias)
        return batch

    async def _invalidate(self, pairs: Iterable[tuple[str, str]]) -> None:
        """Origin Redis invalidation, grouped into one DEL per domain."""
        by_domain: dict[str, list[str]] = {}
        for alias, domain in pairs:
            by_domain.setdefault(domain, []).append(alias)
        for domain, aliases in by_domain.items():
            await self._url_cache.invalidate_many(aliases, domain)

    def _edge_flush(
        self,
        *,
        purge_keys: list[str] | None = None,
        put_entries: list[tuple[str, str]] | None = None,
    ) -> None:
        """Detached, grouped edge reconciliation.

        Never awaited in-request — a remote API must not set a bulk
        endpoint's latency — and grouped per flush rather than per item
        (a batch normally produces one flush; a partially-failed expiry
        batch can produce one per slice). Best-effort like all edge
        writes: a missed purge is bounded by the entries' own TTLs.
        """
        if self._kv is None or not (purge_keys or put_entries):
            return
        task = asyncio.create_task(
            self._flush_edge(purge_keys or [], put_entries or [])
        )
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _flush_edge(
        self, purge_keys: list[str], put_entries: list[tuple[str, str]]
    ) -> None:
        try:
            if put_entries:
                await self._kv.bulk_put(
                    put_entries, expiration_ttl=self._og_ttl_seconds
                )
            if purge_keys:
                await self._kv.bulk_delete(purge_keys)
            log.info(
                "urls_bulk_edge_flush",
                purged=len(purge_keys),
                put=len(put_entries),
            )
        except Exception:
            log.exception("urls_bulk_edge_flush_failed")

    def _system_domain_keys(self, docs: list[UrlV2Doc]) -> list[str]:
        """Edge KV keys for the system-domain slice of *docs*.

        Tenant links never have KV entries (the worker only fronts the
        system domain), so only system-domain keys are ever purged. This
        purge is delete-only by design: a deleted entry re-promotes next
        hot window, while an overwrite could corrupt a plain link's
        promoted redirect entry.
        """
        return [
            cache_key(doc.domain, doc.alias)
            for doc in docs
            if doc.domain == self._system_default_domain
        ]

    def _og_put_entries(self, docs: list[UrlV2Doc], **updates) -> list[tuple[str, str]]:
        """Fresh ``og_only`` entries for the og-links in *docs*, rendered
        from the post-write state (``updates`` is what the write set).

        Eligibility is judged on the MERGED doc: a domain move onto the
        system default begins KV management for an og-link (single-item
        parity — ``sync`` puts whenever the post-write domain is the
        system default and the link is an ACTIVE og-link).
        """
        entries = []
        for doc in docs:
            merged = doc.model_copy(update=updates)
            if (
                merged.domain == self._system_default_domain
                and merged.meta_tags is not None
                and merged.status == UrlStatus.ACTIVE
            ):
                entries.append(build_og_entry(UrlCacheData.from_v2_doc(merged)))
        return entries

    # ── ops ──────────────────────────────────────────────────────────────

    async def bulk_delete(
        self, ids: list[ObjectId], owner_id: ObjectId
    ) -> BulkUrlOperationResponse:
        """Delete exactly *ids* (the owned subset). Mirrors
        ``UrlService.delete`` per item: irreversible, alias freed,
        BLOCKED refused, cache + og entry cleaned up."""
        batch = await self._load(
            ids, owner_id, blocked_message="Cannot delete a blocked URL"
        )
        docs = batch.pending
        if not docs:
            return batch.report(op="delete", user_id=owner_id)

        try:
            await self._url_repo.delete_by_ids_and_owner(
                [doc.id for doc in docs], owner_id
            )
        except PyMongoError:
            log.exception("urls_bulk_delete_write_failed", user_id=str(owner_id))
            await self._attribute_delete_failure(batch, docs, owner_id)
            return batch.report(op="delete", user_id=owner_id)

        await self._finish_deleted(batch, docs, owner_id)
        return batch.report(op="delete", user_id=owner_id)

    async def _finish_deleted(
        self, batch: BulkBatch, docs: list[UrlV2Doc], owner_id: ObjectId
    ) -> None:
        """Verdicts + side effects for docs that are confirmed gone."""
        for doc in docs:
            batch.ok(doc.id, alias=doc.alias)
        await self._invalidate((doc.alias, doc.domain) for doc in docs)
        self._edge_flush(purge_keys=self._system_domain_keys(docs))
        await self._emit_deleted(docs)
        for doc in docs:
            log.info(
                "url_deleted",
                url_id=str(doc.id),
                short_code=doc.alias,
                user_id=str(owner_id),
            )

    async def _attribute_delete_failure(
        self, batch: BulkBatch, docs: list[UrlV2Doc], owner_id: ObjectId
    ) -> None:
        """The one delete_many threw — re-query which ids still exist so
        every row is the exact truth: gone → ok (side effects included),
        survivors → internal. Only if even the re-query fails do we fall
        back to not_attempted."""
        try:
            survivors = {
                doc.id
                for doc in await self._url_repo.find_by_ids_and_owner(
                    [doc.id for doc in docs], owner_id
                )
            }
        except PyMongoError:
            for doc in docs:
                batch.reject(
                    doc.id,
                    "not_attempted",
                    "Processing aborted before completion",
                    alias=doc.alias,
                )
            return
        gone = [doc for doc in docs if doc.id not in survivors]
        if gone:
            await self._finish_deleted(batch, gone, owner_id)
        for doc in docs:
            if doc.id in survivors:
                batch.reject(doc.id, "internal", _INTERNAL_MSG, alias=doc.alias)

    async def bulk_set_status(
        self, ids: list[ObjectId], status: UrlStatus, owner_id: ObjectId
    ) -> BulkUrlOperationResponse:
        """Activate/deactivate exactly *ids*. Mirrors the single-item
        ``status`` handler: same-status items are success no-ops with no
        write (and no ``updated_at`` bump); EXPIRED links set to ACTIVE
        reactivate."""
        now = datetime.now(timezone.utc)
        batch = await self._load(
            ids, owner_id, blocked_message="Cannot modify a blocked URL"
        )
        pending = batch.pending
        changed = [doc for doc in pending if doc.status != status]
        for doc in pending:
            if doc.status == status:
                batch.ok(doc.id, alias=doc.alias)
        if not changed:
            return batch.report(op="set_status", user_id=owner_id)

        set_ops = {"status": status, "updated_at": now}
        if not await self._apply_update(batch, changed, set_ops, owner_id):
            return batch.report(op="set_status", user_id=owner_id)

        await self._invalidate((doc.alias, doc.domain) for doc in changed)
        if status == UrlStatus.INACTIVE:
            # Deactivation is the takedown gesture: purge og entries
            # (write-through parity) AND hot-promoted redirect entries so
            # the edge stops serving within seconds, not TTL.
            self._edge_flush(purge_keys=self._system_domain_keys(changed))
        else:
            # Reactivated og-links re-enter edge serving (sync's ACTIVE
            # branch); plain links just re-promote when hot again.
            self._edge_flush(put_entries=self._og_put_entries(changed, status=status))
        self._log_updated(changed, set_ops, owner_id)
        await self._emit_updated(changed, set_ops)
        return batch.report(op="set_status", user_id=owner_id)

    async def bulk_set_expiry(
        self,
        ids: list[ObjectId],
        expire_after: datetime | None,
        owner_id: ObjectId,
    ) -> BulkUrlOperationResponse:
        """Set/clear expiry on exactly *ids*. Mirrors the single-item
        ``expire_after`` handler (past values are an envelope-level 400 —
        it is one value for the whole batch) and ``_auto_reactivate``:
        EXPIRED links whose expiry is extended or cleared come back
        ACTIVE.

        Raises:
            ValidationError: ``expire_after`` is in the past.
        """
        now = datetime.now(timezone.utc)
        if expire_after is not None and expire_after <= now:
            raise ValidationError(
                "expire_after must be in the future", field="expire_after"
            )

        batch = await self._load(
            ids, owner_id, blocked_message="Cannot modify a blocked URL"
        )
        pending = batch.pending
        if expire_after is None:
            # Clearing an already-clear link is a no-op, same as the
            # single-item handler's truthiness guard.
            changed = [doc for doc in pending if doc.expire_after]
        else:
            # Same comparison expression as the handler — including its
            # aware-vs-naive inequality behavior — so bulk can't drift.
            changed = [doc for doc in pending if doc.expire_after != expire_after]
        changed_ids = {doc.id for doc in changed}
        for doc in pending:
            if doc.id not in changed_ids:
                batch.ok(doc.id, alias=doc.alias)
        if not changed:
            return batch.report(op="set_expiry", user_id=owner_id)

        # _auto_reactivate parity: the field is in the ops and the value
        # is future-or-cleared by construction, so every EXPIRED item in
        # the changed slice reactivates. Explicit status never rides this
        # op, so the "caller set status" guard can't trigger.
        reactivate = [doc for doc in changed if doc.status == UrlStatus.EXPIRED]
        plain = [doc for doc in changed if doc.status != UrlStatus.EXPIRED]

        plain_ops = {"expire_after": expire_after, "updated_at": now}
        react_ops = plain_ops | {"status": UrlStatus.ACTIVE}
        if plain and not await self._apply_update(batch, plain, plain_ops, owner_id):
            plain = []
        if reactivate and not await self._apply_update(
            batch, reactivate, react_ops, owner_id
        ):
            reactivate = []
        applied = plain + reactivate
        if not applied:
            return batch.report(op="set_expiry", user_id=owner_id)

        await self._invalidate((doc.alias, doc.domain) for doc in applied)
        # Expiry itself is not og-relevant (og_html doesn't encode it),
        # but reactivation is a status change: those og entries re-enter
        # edge serving, same as bulk_set_status's ACTIVE branch.
        self._edge_flush(
            put_entries=self._og_put_entries(
                reactivate, status=UrlStatus.ACTIVE, expire_after=expire_after
            )
        )
        if plain:
            self._log_updated(plain, plain_ops, owner_id)
            await self._emit_updated(plain, plain_ops)
        if reactivate:
            self._log_updated(reactivate, react_ops, owner_id)
            await self._emit_updated(reactivate, react_ops)
        return batch.report(op="set_expiry", user_id=owner_id)

    async def bulk_move_domain(
        self,
        ids: list[ObjectId],
        domain: str | None,
        owner_id: ObjectId,
    ) -> BulkUrlOperationResponse:
        """Move exactly *ids* to *domain* (None = system default).

        Mirrors the single-item domain handler per item: already-on-target
        is a success no-op with no write, a reserved alias cannot smuggle
        onto the system default (``validation_error``), and the alias must
        be free on the target namespace (``conflict``) — availability is
        the SAME check the single-item path runs (v2 + legacy + emoji on
        the system default), not just the v2 unique index.

        Unlike the other bulk ops this one writes per item, sequentially,
        in request order: items interact (two links with the same alias
        moving to one target — first wins, deterministically), and the
        ``(domain, alias)`` unique index makes one update_many unable to
        attribute a mid-flight collision. ``DuplicateKeyError`` is the
        race backstop when a third party claims the alias between the
        availability check and the write. Target ownership is the route's
        envelope precondition; the service treats the value as opaque,
        same as the single-item split.
        """
        now = datetime.now(timezone.utc)
        target = domain or self._system_default_domain
        batch = await self._load(
            ids, owner_id, blocked_message="Cannot modify a blocked URL"
        )
        set_ops = {"domain": target, "updated_at": now}
        moved: list[UrlV2Doc] = []
        claimed: set[str] = set()  # aliases granted on the target this batch
        for doc in batch.pending:
            if doc.domain == target:
                batch.ok(doc.id, alias=doc.alias)  # no-op, nothing written
                continue
            if target == self._system_default_domain and is_reserved_alias(doc.alias):
                log.info(
                    "url_domain_move_alias_reserved",
                    short_code=doc.alias,
                    from_domain=doc.domain,
                )
                batch.reject(
                    doc.id,
                    "validation_error",
                    f"Alias '{doc.alias}' is reserved on {target}",
                    alias=doc.alias,
                )
                continue
            if (
                doc.alias in claimed
                or not await self._url_service.check_alias_available(
                    doc.alias, domain=target
                )
            ):
                self._log_move_conflict(doc, target)
                batch.reject(
                    doc.id,
                    "conflict",
                    f"Alias '{doc.alias}' is already in use on {target}",
                    alias=doc.alias,
                )
                continue
            try:
                await self._url_repo.update_by_ids_and_owner(
                    [doc.id], owner_id, set_ops
                )
            except DuplicateKeyError:
                # Race backstop: the alias was claimed on the target between
                # the availability check and the write — the unique index is
                # the authoritative arbiter.
                self._log_move_conflict(doc, target)
                batch.reject(
                    doc.id,
                    "conflict",
                    f"Alias '{doc.alias}' is already in use on {target}",
                    alias=doc.alias,
                )
                continue
            except PyMongoError:
                log.exception(
                    "urls_bulk_move_write_failed",
                    user_id=str(owner_id),
                    url_id=str(doc.id),
                )
                batch.reject(doc.id, "internal", _INTERNAL_MSG, alias=doc.alias)
                # The write's outcome is unknown — clear both keys so a
                # spurious miss re-reads truth rather than serving lies.
                await self._invalidate([(doc.alias, doc.domain), (doc.alias, target)])
                continue
            claimed.add(doc.alias)
            moved.append(doc)
            batch.ok(doc.id, alias=doc.alias)

        if moved:
            # Both keys per item: the pre-move key must not serve stale and
            # the post-move key must not have been populated by a racing
            # read (single-item parity: update() clears old + new).
            pairs: list[tuple[str, str]] = []
            for doc in moved:
                pairs.append((doc.alias, doc.domain))
                pairs.append((doc.alias, target))
            await self._invalidate(pairs)
            # Old system-domain keys drop (og entries re-home; plain links'
            # promoted entries must not serve an alias that moved away —
            # same purge the single-item path fires on a domain change).
            # og-links landing on the system default begin KV management.
            self._edge_flush(
                purge_keys=self._system_domain_keys(moved),
                put_entries=self._og_put_entries(moved, domain=target),
            )
            self._log_updated(moved, set_ops, owner_id)
            await self._emit_updated(moved, set_ops)
            for doc in moved:
                log.info(
                    "url_domain_moved",
                    url_id=str(doc.id),
                    short_code=doc.alias,
                    from_domain=doc.domain,
                    to_domain=target,
                    user_id=str(owner_id),
                )
        return batch.report(op="move_domain", user_id=owner_id)

    def _log_move_conflict(self, doc: UrlV2Doc, target: str) -> None:
        log.info(
            "url_domain_move_alias_conflict",
            short_code=doc.alias,
            from_domain=doc.domain,
            to_domain=target,
        )

    async def bulk_tag(
        self,
        ids: list[ObjectId],
        add: list[ObjectId],
        remove: list[ObjectId],
        owner_id: ObjectId,
    ) -> BulkUrlOperationResponse:
        """Retag exactly *ids*: per item ``(existing minus remove) plus add``.

        ``add`` must be the owner's tags (envelope 400 otherwise, nothing
        attempted). Unchanged items are success no-ops with no write; an
        item whose result would exceed the per-link cap reports
        ``validation_error``. One set-based pipeline update does the remove
        and the add together, so a crash can never leave a link with its
        tags removed but not added. Tags are not in the redirect cache
        projection, so no cache or edge work.
        """
        if add:
            # Fail closed, like the single-item path: no tag service, no vouching.
            if self._tag_service is None:
                raise ValidationError("tags are not available", field="add")
            await self._tag_service.assert_owned(owner_id, add, field="add")
        now = datetime.now(timezone.utc)
        batch = await self._load(
            ids, owner_id, blocked_message="Cannot modify a blocked URL"
        )
        changed: list[tuple[UrlV2Doc, list[ObjectId]]] = []
        for doc in batch.pending:
            kept = [t for t in doc.tag_ids if t not in remove]
            next_tags = kept + [t for t in add if t not in kept]
            if next_tags == doc.tag_ids:
                batch.ok(doc.id, alias=doc.alias)
            elif len(next_tags) > TAGS_MAX_PER_LINK:
                batch.reject(
                    doc.id,
                    "validation_error",
                    f"a link can carry at most {TAGS_MAX_PER_LINK} tags",
                    alias=doc.alias,
                )
            else:
                changed.append((doc, next_tags))
        if not changed:
            return batch.report(op="tag", user_id=owner_id)

        doc_ids = [doc.id for doc, _ in changed]
        # Aggregation-pipeline update: keep what is not removed, append what
        # is added and not already kept. Order-preserving, one write.
        kept = {
            "$filter": {
                "input": {"$ifNull": ["$tag_ids", []]},
                "cond": {"$not": {"$in": ["$$this", remove]}},
            }
        }
        update = [
            {
                "$set": {
                    "tag_ids": {
                        "$let": {
                            "vars": {"kept": kept},
                            "in": {
                                "$concatArrays": [
                                    "$$kept",
                                    {
                                        "$filter": {
                                            "input": add,
                                            "cond": {
                                                "$not": {"$in": ["$$this", "$$kept"]}
                                            },
                                        }
                                    },
                                ]
                            },
                        }
                    },
                    "updated_at": now,
                }
            }
        ]
        try:
            await self._url_repo.apply_by_ids_and_owner(doc_ids, owner_id, update)
        except PyMongoError:
            log.exception(
                "urls_bulk_update_write_failed",
                user_id=str(owner_id),
                fields=["tag_ids"],
            )
            for doc, _ in changed:
                batch.reject(doc.id, "internal", _INTERNAL_MSG, alias=doc.alias)
            return batch.report(op="tag", user_id=owner_id)

        for doc, next_tags in changed:
            batch.ok(doc.id, alias=doc.alias)
            set_ops = {"tag_ids": next_tags, "updated_at": now}
            self._log_updated([doc], set_ops, owner_id)
            await self._emit_updated([doc], set_ops)
        return batch.report(op="tag", user_id=owner_id)

    # ── write helpers ────────────────────────────────────────────────────

    async def _apply_update(
        self,
        batch: BulkBatch,
        docs: list[UrlV2Doc],
        set_ops: dict,
        owner_id: ObjectId,
    ) -> bool:
        """One update_many over *docs*; verdicts either way.

        On failure the slice reports ``internal`` (update_many partial
        application isn't attributable the way delete's re-query is) and
        its Redis keys are invalidated anyway — some docs may have been
        modified, and a spurious cache miss re-reads truth while a stale
        entry serves lies.
        """
        try:
            await self._url_repo.update_by_ids_and_owner(
                [doc.id for doc in docs], owner_id, set_ops
            )
        except PyMongoError:
            log.exception(
                "urls_bulk_update_write_failed",
                user_id=str(owner_id),
                fields=list(set_ops.keys()),
            )
            for doc in docs:
                batch.reject(doc.id, "internal", _INTERNAL_MSG, alias=doc.alias)
            await self._invalidate((doc.alias, doc.domain) for doc in docs)
            self._edge_flush(purge_keys=self._system_domain_keys(docs))
            return False
        for doc in docs:
            batch.ok(doc.id, alias=doc.alias)
        return True

    async def _emit_updated(self, docs: list[UrlV2Doc], set_ops: dict) -> None:
        """One link.updated per changed item — post-state snapshot plus the
        same changes map the single-item producer builds."""
        for doc in docs:
            owner = link_owner_id(doc)
            if owner is None:
                continue
            merged = doc.model_dump(by_alias=True)
            merged.update(set_ops)
            merged["_id"] = doc.id
            post_doc = UrlV2Doc.from_mongo(merged)
            await self._events.emit(
                DomainEvent(
                    type="link.updated",
                    owner_id=owner,
                    data={
                        "link": link_snapshot(post_doc),
                        "changes": event_changes(doc, set_ops),
                    },
                )
            )

    async def _emit_deleted(self, docs: list[UrlV2Doc]) -> None:
        for doc in docs:
            owner = link_owner_id(doc)
            if owner is None:
                continue
            await self._events.emit(
                DomainEvent(
                    type="link.deleted",
                    owner_id=owner,
                    data={"link": link_snapshot(doc)},
                )
            )

    def _log_updated(
        self, docs: list[UrlV2Doc], set_ops: dict, owner_id: ObjectId
    ) -> None:
        """Per-item events identical to the single-item path's, so logs,
        telemetry, and future webhooks can't tell bulk from a loop."""
        fields_changed = list(set_ops.keys())
        for doc in docs:
            log.info(
                "url_updated",
                url_id=str(doc.id),
                short_code=doc.alias,
                user_id=str(owner_id),
                fields_changed=fields_changed,
            )
