"""Response DTO for GET /api/v1/expand."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from schemas.dto.base import ResponseBase


class ChainHop(ResponseBase):
    url: str
    status: int | None = Field(
        default=None, description="HTTP status; null when the hop never answered."
    )
    https: bool


class WebRiskVerdict(ResponseBase):
    checked: bool
    threats: list[str] = Field(default_factory=list)


class ExpandResponse(ResponseBase):
    """A URL's redirect chain, every hop listed in order.

    ``blocklist_match`` is the only safety claim: whether any hop matches
    the abuse blocklist spoo.me enforces at link creation.
    """

    url: str = Field(description="The URL that was requested.")
    final_url: str
    final_status: int | None = None
    truncated: bool = Field(description="Chain stopped at the redirect cap.")
    hops: list[ChainHop]
    blocklist_match: bool
    web_risk: WebRiskVerdict | None = Field(
        default=None,
        description="Google Web Risk verdict for the final URL; null when the check didn't run.",
    )
    fetched_at: datetime
