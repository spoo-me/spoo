"""
Repository for the ``custom_domains`` MongoDB collection.

All methods are async and return typed Pydantic document models.
Errors are handled by BaseRepository — domain methods delegate to shared
CRUD helpers.
"""

from __future__ import annotations

from datetime import datetime, timezone

from bson import ObjectId
from pymongo.errors import PyMongoError

from infrastructure.logging import get_logger
from repositories.base import BaseRepository
from schemas.enums.domain_status import DomainStatus
from schemas.models.custom_domain import CustomDomainDoc

log = get_logger(__name__)


def _canonical(fqdn: str) -> str:
    """Cheap normalisation for lookup parameters.

    Persisted docs are validated through ``normalise_fqdn`` (strict) before
    insert, so they're already canonical. Lookups normalise too — different
    callers (DTO-validated input, raw middleware string, ops mongosh
    input) reach the same row regardless of case or trailing dots. Kept
    cheap (no regex) because lookup syntax is the caller's job, not ours.
    """
    return str(fqdn).strip().lower().rstrip(".")


class CustomDomainRepository(BaseRepository[CustomDomainDoc]):
    async def find_by_id(self, domain_id: ObjectId) -> CustomDomainDoc | None:
        """Find a domain by its ObjectId."""
        return await self._find_one({"_id": domain_id})

    async def find_by_fqdn(self, fqdn: str) -> CustomDomainDoc | None:
        """Find a domain by fqdn (any status). Used by uniqueness checks."""
        return await self._find_one({"fqdn": _canonical(fqdn)})

    async def find_active_by_fqdn(self, fqdn: str) -> CustomDomainDoc | None:
        """Find a domain by fqdn, scoped to ACTIVE only.

        Used by the Caddy ask endpoint — we only mint certs for verified,
        currently-active domains.
        """
        return await self._find_one(
            {
                "fqdn": _canonical(fqdn),
                "status": DomainStatus.ACTIVE,
                "paused_by_limit": {"$ne": True},
            }
        )

    async def find_blocking_by_fqdn(self, fqdn: str) -> CustomDomainDoc | None:
        """Find a doc that would block re-registration of *fqdn*.

        Returns any doc whose status is not REVOKED. REVOKED docs are
        terminal records of past registrations — they don't reserve the
        fqdn for future use (no cooldown). Used by uniqueness enforcement
        in the service layer.
        """
        return await self._find_one(
            {"fqdn": _canonical(fqdn), "status": {"$ne": DomainStatus.REVOKED}}
        )

    async def list_by_owner(
        self,
        owner_id: ObjectId,
        skip: int,
        limit: int,
    ) -> list[CustomDomainDoc]:
        """Return a page of domains owned by *owner_id*, newest first."""
        try:
            cursor = (
                self._col.find({"owner_id": owner_id})
                .sort("created_at", -1)
                .skip(skip)
                .limit(limit)
            )
            docs = await cursor.to_list(length=limit)
            return [CustomDomainDoc.from_mongo(d) for d in docs]  # type: ignore[misc]
        except PyMongoError as exc:
            log.error(
                "repo_list_by_owner_failed",
                collection=self._collection_name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    async def list_live_by_owner(self, owner_id: ObjectId) -> list[CustomDomainDoc]:
        """Every non-revoked domain of *owner_id*, oldest first."""
        cursor = self._col.find(
            {"owner_id": owner_id, "status": {"$ne": DomainStatus.REVOKED.value}}
        ).sort("created_at", 1)
        docs = await cursor.to_list(length=None)
        return [CustomDomainDoc.from_mongo(d) for d in docs]  # type: ignore[misc]

    async def set_paused_by_limit(self, ids: list[ObjectId], paused: bool) -> int:
        result = await self._col.update_many(
            {"_id": {"$in": ids}},
            {
                "$set": {
                    "paused_by_limit": paused,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        return result.modified_count

    async def count_by_owner(self, owner_id: ObjectId) -> int:
        """Count all domains owned by *owner_id* (any status)."""
        return await self._count({"owner_id": owner_id})

    async def insert(self, doc: dict) -> ObjectId:
        """Insert a new domain document. Returns the inserted ``_id``."""
        return await self._insert(doc)

    async def delete_by_id(self, domain_id: ObjectId) -> bool:
        """Hard-delete a domain doc. Used to roll back a failed registration."""
        return await self._delete({"_id": domain_id})

    async def update_status(
        self,
        domain_id: ObjectId,
        new_status: DomainStatus,
        *,
        last_verification_error: str | None = None,
        bump_last_verified_at: bool = False,
    ) -> bool:
        """Persist a status transition.

        Always sets ``updated_at`` to ``now``. Optionally records the latest
        verification error message and bumps ``last_verified_at`` (only set
        on successful checks — failed re-verify keeps the prior timestamp).

        Returns True if the document existed and was updated.
        """
        now = datetime.now(timezone.utc)
        ops: dict = {
            "$set": {
                "status": new_status,
                "updated_at": now,
                "last_verification_error": last_verification_error,
            }
        }
        if bump_last_verified_at:
            ops["$set"]["last_verified_at"] = now
        return await self._update({"_id": domain_id}, ops)

    async def set_eviction_pending(
        self,
        domain_id: ObjectId,
        pending: bool,
        *,
        error: str | None = None,
    ) -> bool:
        """Persist whether the edge still holds a stale cert for this domain.

        Set ``pending=True`` after a SUSPEND/REVOKE if the edge call failed,
        ``False`` after a successful eviction (initial or worker retry).
        ``error`` records the latest failure reason for ops visibility;
        cleared (set to None) on success.
        """
        return await self._update(
            {"_id": domain_id},
            {
                "$set": {
                    "eviction_pending": pending,
                    "last_eviction_error": error,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )

    async def update_routing(
        self,
        domain_id: ObjectId,
        ops: dict,
    ) -> bool:
        """Persist a partial update to the per-domain routing config.

        ``ops`` is the already-built ``{field: value}`` dict from the service —
        only keys the caller explicitly set are present. ``None`` values are
        passed through (a ``$set: {field: None}`` clears the stored value).
        """
        if not ops:
            return False
        ops = {**ops, "updated_at": datetime.now(timezone.utc)}
        return await self._update({"_id": domain_id}, {"$set": ops})

    async def update_edge_metadata(
        self,
        domain_id: ObjectId,
        *,
        cf_hostname_id: str | None = None,
        cf_status: str | None = None,
        cf_ssl_status: str | None = None,
        dns_instructions: list[dict[str, str]] | None = None,
    ) -> bool:
        """Persist edge-backend bookkeeping (CF hostname id + status snapshots).

        Only fields explicitly passed (non-None) are written so the worker
        sync path can refresh status without clobbering the id, and the
        create path can stamp the id without overwriting later sync results.
        """
        ops: dict = {}
        if cf_hostname_id is not None:
            ops["cf_hostname_id"] = cf_hostname_id
        if cf_status is not None:
            ops["cf_status"] = cf_status
        if cf_ssl_status is not None:
            ops["cf_ssl_status"] = cf_ssl_status
        if dns_instructions is not None:
            ops["dns_instructions"] = dns_instructions
        if not ops:
            return False
        ops["updated_at"] = datetime.now(timezone.utc)
        return await self._update({"_id": domain_id}, {"$set": ops})

    async def find_pending(
        self, created_before: datetime, limit: int
    ) -> list[CustomDomainDoc]:
        """PENDING docs older than ``created_before`` — for the background poller."""
        try:
            cursor = (
                self._col.find(
                    {
                        "status": DomainStatus.PENDING,
                        "is_system_default": {"$ne": True},
                        "created_at": {"$lt": created_before},
                    }
                )
                .sort("created_at", 1)
                .limit(limit)
            )
            docs = await cursor.to_list(length=limit)
            return [CustomDomainDoc.from_mongo(d) for d in docs]  # type: ignore[misc]
        except PyMongoError as exc:
            log.error(
                "repo_find_pending_failed",
                collection=self._collection_name,
                error=str(exc),
            )
            raise

    async def find_stale_active(
        self, older_than: datetime, limit: int
    ) -> list[CustomDomainDoc]:
        """Return ACTIVE domains whose ``last_verified_at`` is older than the cutoff.

        Used by the background re-verify worker to pick the next batch. Sorted
        oldest-first so the most stale gets re-checked first.
        """
        try:
            cursor = (
                self._col.find(
                    {
                        "status": DomainStatus.ACTIVE,
                        "is_system_default": {"$ne": True},
                        "$or": [
                            {"last_verified_at": {"$lt": older_than}},
                            {"last_verified_at": None},
                        ],
                    }
                )
                .sort("last_verified_at", 1)
                .limit(limit)
            )
            docs = await cursor.to_list(length=limit)
            return [CustomDomainDoc.from_mongo(d) for d in docs]  # type: ignore[misc]
        except PyMongoError as exc:
            log.error(
                "repo_find_stale_active_failed",
                collection=self._collection_name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise
