"""Request DTOs for per-user page layouts."""

from __future__ import annotations

import json
from typing import Any

from pydantic import Field, field_validator

from app.schemas.dto.base import RequestBase

# Explicit cap far under the global 1 MiB body limit: a 30-widget dashboard
# doc serializes to ~4 KiB, so 32 KiB is ~8x headroom.
MAX_LAYOUT_BYTES = 32 * 1024


class PutLayoutRequest(RequestBase):
    layout: dict[str, Any] = Field(
        description=(
            "Opaque layout document owned by the client. The server stores it "
            "verbatim; versioning happens inside the document."
        ),
        examples=[{"version": 1, "widgets": []}],
    )

    @field_validator("layout", mode="after")
    @classmethod
    def _cap_size(cls, v: dict[str, Any]) -> dict[str, Any]:
        raw = json.dumps(v, separators=(",", ":"), ensure_ascii=False)
        if len(raw.encode("utf-8")) > MAX_LAYOUT_BYTES:
            raise ValueError("layout document too large (max 32 KiB)")
        return v
