"""Tag management: the per-account registry links point at by id."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.errors import ConflictError, NotFoundError, ValidationError
from app.infrastructure.logging import get_logger
from app.schemas.models.tag import AUTO_COLORS, TAGS_MAX_PER_OWNER, TagColor, TagDoc

if TYPE_CHECKING:
    from app.repositories.tag_repository import TagRepository
    from app.repositories.url_repository import UrlRepository

log = get_logger(__name__)


class TagService:
    def __init__(self, tag_repo: TagRepository, url_repo: UrlRepository) -> None:
        self._tags = tag_repo
        self._urls = url_repo

    # ── registry ────────────────────────────────────────────────────────

    async def create(
        self,
        owner_id: ObjectId,
        name: str,
        color: TagColor | None,
        icon: str = "tag",
    ) -> TagDoc:
        if await self._tags.count_by_owner(owner_id) >= TAGS_MAX_PER_OWNER:
            raise ValidationError(
                f"an account can have at most {TAGS_MAX_PER_OWNER} tags", field="name"
            )
        if color is None:
            color = self._least_used_color(await self._tags.list_by_owner(owner_id))
        now = datetime.now(timezone.utc)
        doc = TagDoc(
            owner_id=owner_id, name=name, color=color, icon=icon, created_at=now
        )
        try:
            doc.id = await self._tags.insert(
                doc.model_dump(by_alias=True, exclude={"id"})
            )
        except DuplicateKeyError:
            raise ConflictError(f"you already have a tag named '{name}'") from None
        log.info("tag_created", user_id=str(owner_id), tag_id=str(doc.id), color=color)
        return doc

    async def update(
        self,
        owner_id: ObjectId,
        tag_id: ObjectId,
        *,
        name: str | None,
        color: TagColor | None,
        icon: str | None = None,
    ) -> TagDoc:
        existing = await self._tags.find_by_id_and_owner(tag_id, owner_id)
        if existing is None:
            raise NotFoundError("Tag not found")
        set_ops: dict = {}
        if name is not None and name != existing.name:
            set_ops["name"] = name
        if color is not None and color != existing.color:
            set_ops["color"] = color
        if icon is not None and icon != existing.icon:
            set_ops["icon"] = icon
        if not set_ops:
            return existing
        set_ops["updated_at"] = datetime.now(timezone.utc)
        try:
            await self._tags.update_by_id_and_owner(tag_id, owner_id, set_ops)
        except DuplicateKeyError:
            raise ConflictError(f"you already have a tag named '{name}'") from None
        log.info(
            "tag_updated",
            user_id=str(owner_id),
            tag_id=str(tag_id),
            fields=list(set_ops),
        )
        return existing.model_copy(update=set_ops)

    async def delete(self, owner_id: ObjectId, tag_id: ObjectId) -> int:
        """Delete the tag and strip it from every link. Returns links touched."""
        existing = await self._tags.find_by_id_and_owner(tag_id, owner_id)
        if existing is None:
            raise NotFoundError("Tag not found")
        links_updated = await self._urls.pull_tag_id_by_owner(owner_id, tag_id)
        await self._tags.delete_by_id_and_owner(tag_id, owner_id)
        log.info(
            "tag_deleted",
            user_id=str(owner_id),
            tag_id=str(tag_id),
            links_updated=links_updated,
        )
        return links_updated

    async def link_count(self, owner_id: ObjectId, tag_id: ObjectId) -> int:
        return await self._urls.count_by_query(
            {"owner_id": owner_id, "tag_ids": tag_id}
        )

    async def list_with_counts(self, owner_id: ObjectId) -> list[tuple[TagDoc, int]]:
        docs = await self._tags.list_by_owner(owner_id)
        counts = await self._urls.count_tag_ids_by_owner(owner_id)
        return [(doc, counts.get(doc.id, 0)) for doc in docs]

    async def delete_all_for_owner(self, owner_id: ObjectId) -> int:
        """Account-erasure cascade; the links go separately."""
        return await self._tags.delete_by_owner(owner_id)

    # ── lookups the link paths use ──────────────────────────────────────

    async def refs_by_id(
        self, owner_id: ObjectId, tag_ids: list[ObjectId]
    ) -> dict[ObjectId, TagDoc]:
        """Owned tag docs keyed by id, for embedding on link responses."""
        unique = list({i: None for i in tag_ids})
        docs = await self._tags.find_by_ids_and_owner(unique, owner_id)
        return {doc.id: doc for doc in docs}

    async def assert_owned(
        self, owner_id: ObjectId, tag_ids: list[ObjectId], *, field: str = "tag_ids"
    ) -> None:
        """400 if any id is not one of the owner's tags. The one ownership
        check every link write goes through, so the rule cannot fork."""
        if not tag_ids:
            return
        found = {
            d.id for d in await self._tags.find_by_ids_and_owner(tag_ids, owner_id)
        }
        missing = [str(i) for i in tag_ids if i not in found]
        if missing:
            raise ValidationError(f"unknown tag ids: {', '.join(missing)}", field=field)

    async def ids_for_names(
        self, owner_id: ObjectId, names: list[str]
    ) -> list[ObjectId]:
        """Names → owned ids; unknown names resolve to nothing."""
        docs = await self._tags.find_by_names_and_owner(names, owner_id)
        return [d.id for d in docs]

    @staticmethod
    def _least_used_color(existing: list[TagDoc]) -> TagColor:
        used = Counter(doc.color for doc in existing)
        return min(AUTO_COLORS, key=lambda c: (used.get(c, 0), AUTO_COLORS.index(c)))
