"""
Repository for the `clicks` MongoDB time-series collection.

The clicks collection has a strict schema requirement:
  - timeField: "clicked_at"  (datetime)
  - metaField: "meta"        (ClickMeta subdoc with url_id, short_code, owner_id)

All aggregation pipelines are passed in from the service layer — the repository
does not build pipelines itself.
"""

from __future__ import annotations

from typing import Any

from bson import ObjectId
from pymongo.errors import PyMongoError

from app.infrastructure.logging import get_logger
from app.repositories.base import BaseRepository
from app.schemas.models.base import ANONYMOUS_OWNER_ID

log = get_logger(__name__)


class ClickRepository(BaseRepository[None]):
    async def insert(self, doc: dict) -> None:
        """Insert a click document into the time-series collection.

        The caller (click service) is responsible for constructing a valid
        document via ClickDoc.to_mongo(), which guarantees the required
        `meta` and `clicked_at` fields are present.
        """
        try:
            await self._col.insert_one(doc)
        except PyMongoError as exc:
            log.error(
                "repo_insert_failed",
                collection=self._collection_name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    async def delete_by_owner(self, owner_id: ObjectId) -> int:
        """Delete every click the owner's links ever produced (account erasure).

        Time-series collections only allow deletes that filter on the
        metaField, so the predicate MUST stay ``meta.owner_id``-only.
        Refuses the anonymous sentinel — a bug here would mass-delete the
        analytics of every unclaimed link. Returns the number deleted.
        """
        if not owner_id or owner_id == ANONYMOUS_OWNER_ID:
            raise ValueError("owner_id must be a real account id")
        return await self._delete_many({"meta.owner_id": owner_id})

    async def delete_by_url_ids(self, url_ids: list[ObjectId]) -> int:
        """Delete every click on the given links (account erasure).

        Complements ``delete_by_owner``: clicks stamp ``meta.owner_id`` at
        click time, so clicks that landed before a link was claimed still
        carry the anonymous sentinel — only the url_id ties them to the
        erased account. ``meta.url_id`` lives in the metaField subdoc, so
        the predicate satisfies the time-series delete restriction. An
        empty list deletes nothing — same fail-closed spirit as the
        sentinel guard above (an unguarded empty ``$in`` matches nothing in
        Mongo, but returning early keeps the contract explicit).
        """
        if not url_ids:
            return 0
        return await self._delete_many({"meta.url_id": {"$in": url_ids}})

    async def aggregate(self, pipeline: list[dict]) -> list[dict[str, Any]]:
        """Run an aggregation pipeline against the clicks collection.

        The pipeline is built by the stats service (supports $facet for
        multiple simultaneous aggregations). Returns the full result list.
        """
        return await self._aggregate(pipeline)
