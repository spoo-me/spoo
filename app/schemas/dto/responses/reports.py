"""
Response DTOs for the report intake + contact API.

Wire shapes are FROZEN — the Next frontend's report page and contact
form build against these exact fields.
"""

from __future__ import annotations

from pydantic import Field

from app.schemas.dto.base import ResponseBase
from app.schemas.enums.report import RejectionCode


class ContactOkResponse(ResponseBase):
    """Response body for POST /api/v1/contact."""

    ok: bool = True


class RejectedReportItem(ResponseBase):
    """One rejected entry in the per-item breakdown.

    ``index`` refers to the item's position in the submitted ``items``
    array; ``input`` echoes the raw ``code_or_url`` so bulk clients can
    line results up without keeping their own index map.
    """

    index: int
    input: str
    code: RejectionCode


class ReportSubmissionResponse(ResponseBase):
    """Response body for POST /api/v1/reports.

    Bad codes don't sink the batch: accepted items are stored even when
    others are rejected, and the breakdown says which and why.
    """

    submission_id: str = Field(description="Opaque submission reference")
    accepted: int = Field(description="Number of items stored")
    rejected: list[RejectedReportItem] = Field(
        description="Per-item rejections (empty when everything was accepted)",
    )
