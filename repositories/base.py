"""
Generic base repository — Template Method pattern for MongoDB data access.

Subclasses inherit shared CRUD helpers with consistent error handling and
logging.  Domain-specific methods stay on the subclass; only the
try / catch / log / re-raise skeleton lives here.

Usage::

    class UrlRepository(BaseRepository[UrlV2Doc]):
        async def find_by_alias(self, alias: str) -> UrlV2Doc | None:
            return await self._find_one({"alias": alias})
"""

from __future__ import annotations

from typing import Generic, TypeVar

from bson import ObjectId
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.errors import DuplicateKeyError, PyMongoError

from infrastructure.logging import get_logger

log = get_logger(__name__)

T = TypeVar("T")


class BaseRepository(Generic[T]):
    """Generic MongoDB repository with shared CRUD primitives.

    Type parameter ``T`` is the Pydantic document model returned by
    ``_find_one``.  Repositories that don't convert to a model (e.g.
    ``ClickRepository``) can use ``BaseRepository[None]`` and rely on
    the raw helpers instead.
    """

    # Subclasses may set this to override the auto-derived collection name
    # used in log messages.  By default it is read from the collection object.
    _model: type[T] | None = None

    def __init__(self, collection: AsyncCollection) -> None:
        self._col = collection
        self._collection_name: str = collection.name

    # ── Read helpers ────────────────────────────────────────────────────

    async def _find_one(self, query: dict) -> T | None:
        """Find a single document and convert it via ``T.from_mongo()``.

        Returns ``None`` when no document matches.
        """
        try:
            doc = await self._col.find_one(query)
            if doc is None:
                return None
            model_cls = self._resolve_model()
            return model_cls.from_mongo(doc)  # type: ignore[union-attr]
        except PyMongoError as exc:
            log.error(
                "repo_find_one_failed",
                collection=self._collection_name,
                query_keys=list(query.keys()),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    async def _find_many(
        self,
        query: dict,
        *,
        sort: list[tuple[str, int]] | None = None,
        limit: int | None = 500,
    ) -> list[T]:
        """Find matching documents and convert each via ``T.from_mongo()``.
        ``limit=None`` returns them all."""
        try:
            cursor = self._col.find(query)
            if sort:
                cursor = cursor.sort(sort)
            if limit is not None:
                cursor = cursor.limit(limit)
            docs = await cursor.to_list(length=limit)
            model_cls = self._resolve_model()
            return [model_cls.from_mongo(d) for d in docs]  # type: ignore[union-attr]
        except PyMongoError as exc:
            log.error(
                "repo_find_many_failed",
                collection=self._collection_name,
                query_keys=list(query.keys()),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    async def _find_one_raw(
        self, query: dict, projection: dict | None = None
    ) -> dict | None:
        """Find a single document and return the raw dict (no model conversion)."""
        try:
            return await self._col.find_one(query, projection)
        except PyMongoError as exc:
            log.error(
                "repo_find_one_raw_failed",
                collection=self._collection_name,
                query_keys=list(query.keys()),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    # ── Write helpers ───────────────────────────────────────────────────

    async def _insert(self, doc: dict) -> ObjectId:
        """Insert a document. Returns the inserted ``_id``."""
        try:
            result = await self._col.insert_one(doc)
            return result.inserted_id
        except DuplicateKeyError as exc:
            log.warning(
                "repo_insert_duplicate",
                collection=self._collection_name,
                error=str(exc),
            )
            raise
        except PyMongoError as exc:
            log.error(
                "repo_insert_failed",
                collection=self._collection_name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    async def _update(self, query: dict, ops: dict) -> bool:
        """Apply an update operation. Returns ``True`` if a document matched."""
        try:
            result = await self._col.update_one(query, ops)
            return result.matched_count > 0
        except PyMongoError as exc:
            log.error(
                "repo_update_failed",
                collection=self._collection_name,
                query_keys=list(query.keys()),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    async def _update_modified(self, query: dict, ops: dict) -> bool:
        """Apply an update operation. Returns ``True`` only if a document was actually changed.

        Use instead of ``_update`` when the caller needs to distinguish
        "matched but unchanged" from "matched and modified" (e.g. conditional
        status transitions that should not trigger side-effects twice).
        """
        try:
            result = await self._col.update_one(query, ops)
            return result.modified_count > 0
        except PyMongoError as exc:
            log.error(
                "repo_update_modified_failed",
                collection=self._collection_name,
                query_keys=list(query.keys()),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    async def _delete(self, query: dict) -> bool:
        """Delete a single document. Returns ``True`` if a document was removed."""
        try:
            result = await self._col.delete_one(query)
            return result.deleted_count > 0
        except PyMongoError as exc:
            log.error(
                "repo_delete_failed",
                collection=self._collection_name,
                query_keys=list(query.keys()),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    async def _delete_many(self, query: dict) -> int:
        """Delete all documents matching *query*. Returns the count of removed documents."""
        try:
            result = await self._col.delete_many(query)
            return result.deleted_count
        except PyMongoError as exc:
            log.error(
                "repo_delete_many_failed",
                collection=self._collection_name,
                query_keys=list(query.keys()),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    # ── Aggregate / count helpers ───────────────────────────────────────

    async def _count(self, query: dict) -> int:
        """Count documents matching *query*."""
        try:
            return await self._col.count_documents(query)
        except PyMongoError as exc:
            log.error(
                "repo_count_failed",
                collection=self._collection_name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    async def _aggregate(self, pipeline: list[dict]) -> list[dict]:
        """Run an aggregation pipeline and return all result documents."""
        try:
            cursor = await self._col.aggregate(pipeline)
            return await cursor.to_list(length=None)
        except PyMongoError as exc:
            log.error(
                "repo_aggregate_failed",
                collection=self._collection_name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    # ── Internal ────────────────────────────────────────────────────────

    def _resolve_model(self) -> type[T]:
        """Return the model class for ``_find_one``.

        Checks ``_model`` class var first, then falls back to the generic
        type argument extracted at class definition time via
        ``__orig_bases__``.
        """
        if self._model is not None:
            return self._model

        # Walk MRO to find BaseRepository[SomeModel]
        for base in type(self).__orig_bases__:  # type: ignore[attr-defined]
            args = getattr(base, "__args__", None)
            if args:
                cls = args[0]
                if cls is not type(None):
                    self._model = cls  # type: ignore[assignment]
                    return cls
        raise TypeError(
            f"{type(self).__name__} has no model type — use _find_one_raw or "
            f"set _model on the class."
        )
