"""Document model for the ``tags`` collection: one row per (owner, name)."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import field_validator

from app.schemas.models.base import MongoBaseModel, PyObjectId
from app.shared.tag_icons import DEFAULT_TAG_ICON, TAG_ICONS, TagIcon
from app.shared.tags import normalise_tag


class TagColor(str, Enum):
    """Fixed palette; the dashboard maps each key to a muted dot colour."""

    GRAY = "gray"
    RED = "red"
    ORANGE = "orange"
    AMBER = "amber"
    GREEN = "green"
    TEAL = "teal"
    BLUE = "blue"
    VIOLET = "violet"
    PINK = "pink"


# The colours an auto-assigned tag may receive; gray is the explicit choice.
AUTO_COLORS: tuple[TagColor, ...] = tuple(c for c in TagColor if c is not TagColor.GRAY)

TAGS_MAX_PER_OWNER = 500


def validate_tag_icon(v: Any) -> str:
    """Omitted (None or "") falls back to the generic tag; else a curated key."""
    if v is None or v == "":
        return DEFAULT_TAG_ICON
    if isinstance(v, TagIcon):
        return v.value
    if not isinstance(v, str) or v not in TAG_ICONS:
        raise ValueError(f"unknown tag icon '{v}'")
    return v


class TagDoc(MongoBaseModel):
    """A user's tag. ``name`` is normalised and unique per owner."""

    owner_id: PyObjectId
    name: str
    color: TagColor = TagColor.GRAY
    # Curated lucide key (shared.tag_icons); every tag has one.
    icon: str = DEFAULT_TAG_ICON
    created_at: datetime
    updated_at: datetime | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _normalise_name(cls, v: Any) -> str:
        return normalise_tag(v)

    @field_validator("icon", mode="before")
    @classmethod
    def _known_icon(cls, v: Any) -> str:
        return validate_tag_icon(v)
