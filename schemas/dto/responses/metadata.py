"""Response DTO for GET /api/v1/metadata."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from schemas.dto.base import ResponseBase


class MetadataResponse(ResponseBase):
    """Parsed meta tags of a destination page.

    ``title``/``description``/``image``/``color``/``site_name`` are the
    normalized best-picks (og → twitter → html fallbacks) ready to prefill
    a link's ``meta_tags``; ``og``/``twitter`` carry the raw families.
    """

    url: str = Field(description="The URL that was requested.")
    final_url: str = Field(description="URL after following redirects.")
    title: str | None = None
    description: str | None = None
    image: str | None = Field(default=None, description="Absolute https URL.")
    color: str | None = Field(default=None, description="theme-color if #RRGGBB.")
    site_name: str | None = None
    html_title: str | None = Field(
        default=None, description="Raw <title> text, before og/twitter fallbacks."
    )
    html_description: str | None = Field(
        default=None, description="Plain <meta name=description>, unnormalized."
    )
    favicon: str | None = Field(
        default=None,
        description="Best declared icon (or /favicon.ico); absolute https URL.",
    )
    og: dict[str, str] = Field(default_factory=dict)
    twitter: dict[str, str] = Field(default_factory=dict)
    fetched_at: datetime
