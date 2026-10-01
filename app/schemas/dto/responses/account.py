"""
Response DTOs for account deletion.

AccountDeletionResponse — DELETE /api/v1/me (200)
"""

from __future__ import annotations

from pydantic import Field

from app.schemas.dto.base import ResponseBase, UtcDatetime


class AccountDeletionResponse(ResponseBase):
    """Deletion accepted — the account is now pending erasure."""

    purge_after: UtcDatetime = Field(
        description=(
            "When the grace period ends and the erasure sweep may pick the "
            "account up. Restoring before this instant cancels the deletion."
        ),
        examples=["2026-08-26T00:00:00+00:00"],
    )
