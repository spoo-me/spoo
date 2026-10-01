"""
Request DTO for the public per-link stats endpoint.

PublicStatsBody — POST /api/v1/public/stats/{short_code} (JSON body)

The POST body is the ONLY way a password travels to this endpoint — never
a query param or header, so passwords can't leak into URLs, logs, or
referrers. The body is optional; an absent or empty body means "no
password supplied".
"""

from __future__ import annotations

from pydantic import Field

from app.schemas.dto.base import RequestBase


class PublicStatsBody(RequestBase):
    """Optional JSON body carrying the stats-page password."""

    password: str | None = Field(
        default=None,
        max_length=200,
        description="Password for a password-protected link's stats.",
    )
