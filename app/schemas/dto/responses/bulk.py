"""Response DTOs for bulk URL operations (``POST /api/v1/urls/bulk/*``).

One grammar for every bulk op: a summary plus exactly one result row
per unique requested id. The report is the contract — the summary is
derived from the rows, never tracked separately, and a batch that ran
always answers 200 with this shape even when every item failed
(per-item failures are answers, not errors; 4xx is reserved for
envelope rejection where zero items were attempted).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.schemas.dto.base import ResponseBase

BulkErrorCode = Literal[
    "not_found",
    "forbidden",
    "conflict",
    "validation_error",
    "internal",
    "not_attempted",
]
"""Closed per-item error vocabulary, typed so OpenAPI exposes the enum.

Slugs reuse the API-wide error codes (errors.py) plus the two
bulk-specific ones. Adding a code later is an additive schema bump.
"""


class BulkOperationSummary(ResponseBase):
    """Counts derived from the result rows."""

    total: int = Field(description="Unique ids in the request (after dedupe).")
    succeeded: int = Field(description="Rows with ok=true.")
    failed: int = Field(description="Rows with ok=false.")


class BulkUrlResultRow(ResponseBase):
    """Per-item verdict.

    ``error_code`` reuses the API's error-code slugs so clients share
    one mapping with the single-item routes: ``not_found`` (no such URL
    in your account — someone else's id answers the same, deliberately),
    ``forbidden`` (blocked link), ``conflict``, ``validation_error``,
    plus ``internal`` (unexpected per-item failure, logged server-side)
    and ``not_attempted`` (processing aborted before this item).
    ``error`` is display-safe but not stable; ``error_code`` is the key
    to branch on.
    """

    id: str = Field(description="The requested URL id.")
    alias: str | None = Field(
        default=None,
        description="Echoed when the id resolved to a URL you own; null otherwise.",
    )
    ok: bool = Field(description="Whether the operation succeeded for this id.")
    error_code: BulkErrorCode | None = Field(
        default=None, description="Machine-readable failure cause; null when ok."
    )
    error: str | None = Field(
        default=None, description="Human-readable failure message; null when ok."
    )


class BulkUrlOperationResponse(ResponseBase):
    """Envelope for every bulk URL operation."""

    summary: BulkOperationSummary
    results: list[BulkUrlResultRow] = Field(
        description="One row per unique requested id, in request order."
    )
