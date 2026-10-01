"""
Per-user page layout persistence (dashboard customization).

The layout document is client-owned and stored opaquely: the frontend
versions, validates and migrates it; the server only guarantees durable
per-(user, page) storage. Absence of a doc means "client default".
"""

from __future__ import annotations

from typing import Any

from bson import ObjectId

from app.infrastructure.logging import get_logger
from app.repositories.page_layout_repository import PageLayoutRepository

log = get_logger(__name__)


class PageLayoutService:
    def __init__(self, repo: PageLayoutRepository) -> None:
        self._repo = repo

    async def get_layout(self, user_id: ObjectId, page: str) -> dict[str, Any] | None:
        doc = await self._repo.get(user_id, page)
        return doc.layout if doc else None

    async def put_layout(
        self, user_id: ObjectId, page: str, layout: dict[str, Any]
    ) -> dict[str, Any]:
        await self._repo.upsert(user_id, page, layout)
        log.info("page_layout_saved", user_id=str(user_id), page=page)
        return layout

    async def delete_layout(self, user_id: ObjectId, page: str) -> bool:
        deleted = await self._repo.delete(user_id, page)
        if deleted:
            log.info("page_layout_reset", user_id=str(user_id), page=page)
        return deleted
