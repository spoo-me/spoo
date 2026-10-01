"""Response DTOs for per-user page layouts."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.schemas.dto.base import ResponseBase


class LayoutResponse(ResponseBase):
    layout: dict[str, Any] | None = Field(
        default=None,
        description="Saved layout doc, or null when no override exists",
    )
