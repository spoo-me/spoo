"""Request DTOs for the tags API."""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic.json_schema import SkipJsonSchema

from app.schemas.dto.base import RequestBase
from app.schemas.models.tag import TagColor
from app.shared.tag_icons import TagIcon
from app.shared.tags import TAG_MAX_LENGTH, normalise_tag

_NAME_DESC = (
    f"Tag name, at most {TAG_MAX_LENGTH} characters. Lowercased and trimmed on "
    "write; letters, digits, spaces, `-`, `_` and `.` only. Unique per account."
)
_ICON_DESC = "Icon key from the curated set (lucide names). Defaults to `tag`."
_COLOR_DESC = (
    "One of the palette keys. Omit on create to get the least-used colour "
    "in your account."
)


class CreateTagRequest(RequestBase):
    name: str = Field(
        max_length=TAG_MAX_LENGTH * 2, description=_NAME_DESC, examples=["launch"]
    )
    color: TagColor | None = Field(
        default=None, description=_COLOR_DESC, examples=["violet"]
    )
    icon: TagIcon = Field(
        default=TagIcon("tag"), description=_ICON_DESC, examples=["rocket"]
    )

    @field_validator("name", mode="before")
    @classmethod
    def _norm_name(cls, v: object) -> str:
        return normalise_tag(v)


class UpdateTagRequest(RequestBase):
    """Rename, recolour or change the icon. Omitted fields are left as they are."""

    name: str | None = Field(
        default=None, max_length=TAG_MAX_LENGTH * 2, description=_NAME_DESC
    )
    color: TagColor | None = Field(default=None, description=_COLOR_DESC)
    icon: TagIcon | SkipJsonSchema[None] = Field(default=None, description=_ICON_DESC)

    @field_validator("name", mode="before")
    @classmethod
    def _norm_name(cls, v: object) -> str | None:
        return None if v is None else normalise_tag(v)

    @field_validator("icon", mode="before")
    @classmethod
    def _icon_not_null(cls, v: object) -> object:
        # Omitted keeps the icon; explicit null is refused, every tag has one.
        if v is None:
            raise ValueError("icon cannot be null; every tag has one (default `tag`)")
        return v
