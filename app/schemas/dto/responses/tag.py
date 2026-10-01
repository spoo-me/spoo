"""Response DTOs for the tags API and the tag refs embedded on links."""

from __future__ import annotations

from pydantic import Field

from app.schemas.dto.base import ResponseBase, UtcDatetime
from app.schemas.models.tag import TagColor, TagDoc
from app.shared.tag_icons import TagIcon


class TagRef(ResponseBase):
    """A tag as it appears on a link: enough to render, no counts."""

    id: str = Field(examples=["665f0c2f9e7a4b1d2c3d4e5f"])
    name: str = Field(examples=["launch"])
    color: TagColor = Field(examples=["violet"])
    icon: TagIcon = Field(examples=["rocket"])

    @classmethod
    def from_doc(cls, doc: TagDoc) -> TagRef:
        return cls(id=str(doc.id), name=doc.name, color=doc.color, icon=doc.icon)


class TagResponse(TagRef):
    """A tag on its own endpoints, with its link count."""

    link_count: int = Field(description="Links carrying the tag.", examples=[14])
    created_at: UtcDatetime
    updated_at: UtcDatetime | None = None

    @classmethod
    def from_doc(cls, doc: TagDoc, link_count: int = 0) -> TagResponse:  # type: ignore[override]
        return cls(
            id=str(doc.id),
            name=doc.name,
            color=doc.color,
            icon=doc.icon,
            link_count=link_count,
            created_at=doc.created_at,
            updated_at=doc.updated_at,
        )


class TagListResponse(ResponseBase):
    items: list[TagResponse]


class TagDeleteResponse(ResponseBase):
    deleted: bool = True
    links_updated: int = Field(description="Links the tag was removed from.")
