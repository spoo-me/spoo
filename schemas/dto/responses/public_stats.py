"""
Response DTOs for the public per-link stats endpoint.

PublicStatsResponse — GET|POST /api/v1/public/stats/{short_code} (200)

The wire contract is frozen by the spoo-landing public stats page
(consumed via ``lib/api/public-stats.ts``, mirrored byte-for-byte by its
mock API). ``stats`` carries the same shape as GET /api/v1/stats — the
dynamic ``{metric}_by_{dimension}`` metric keys make it a flexible dict.
DTOs serialize only; all derivation (effective status, long_url
withholding, v1 synthesis) lives in PublicStatsService.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from schemas.dto.base import ResponseBase


class PublicLinkFacts(ResponseBase):
    """Public facts about the link shown above the charts."""

    alias: str
    short_url: str
    # Destination-only-while-active (same rule as the preview page):
    # null unless the effective status is "active"; owner sessions
    # always get it.
    long_url: str | None = None
    # Where an expired link still sends visitors; set only while ``status``
    # is "expired" and the owner set a fallback.
    expired_redirect_url: str | None = None
    created_at: datetime | None = None
    status: Literal["active", "inactive", "expired", "blocked", "scheduled"]
    max_clicks: int | None = None
    block_bots: bool
    password_protected: bool


class PublicStatsResponse(ResponseBase):
    """Response body for GET|POST /api/v1/public/stats/{short_code}."""

    # Which URL generation served the analytics. Emoji aliases collapse to
    # "v1" — they carry v1-shaped (lifetime-dimension) analytics.
    generation: Literal["v1", "v2"]
    link: PublicLinkFacts
    # The owner's plan strips the spoo.me chrome from the public page.
    whitelabel: bool = False
    stats: dict[str, Any] = Field(
        description=(
            "The modern stats wire shape (same as GET /api/v1/stats): "
            "summary, metrics keyed '{metric}_by_{dimension}', time_range, "
            "time_bucket_info, computed_metrics. v1 links carry a "
            "'clicks_by_bots' dimension and no 'city'/'device'; v2 the reverse."
        ),
    )
