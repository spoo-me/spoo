"""Seed the ``custom_domains`` row representing the system default domain.

Idempotent — safe to call on every boot. Owner is ``ANONYMOUS_OWNER_ID``
because the system default isn't user-owned.
"""

from __future__ import annotations

from datetime import datetime, timezone

from bson import ObjectId
from pymongo.asynchronous.database import AsyncDatabase

from app.infrastructure.logging import get_logger
from app.schemas.models.base import ANONYMOUS_OWNER_ID

log = get_logger(__name__)


async def ensure_system_default_domain(db: AsyncDatabase, fqdn: str) -> ObjectId:
    """Upsert the system default domain row. Returns its ``_id``."""
    now = datetime.now(timezone.utc)
    result = await db["custom_domains"].update_one(
        {"fqdn": fqdn},
        {
            "$setOnInsert": {
                "fqdn": fqdn,
                "owner_id": ANONYMOUS_OWNER_ID,
                "status": "active",
                "verification_method": "system",
                "is_system_default": True,
                "created_at": now,
            },
            "$set": {"updated_at": now},
        },
        upsert=True,
    )

    if result.upserted_id is not None:
        log.info("system_default_domain_seeded", fqdn=fqdn, _id=str(result.upserted_id))
        return result.upserted_id

    existing = await db["custom_domains"].find_one({"fqdn": fqdn}, {"_id": 1})
    if existing is None:  # pragma: no cover
        raise RuntimeError(f"system default domain {fqdn!r} vanished after upsert")
    return existing["_id"]
