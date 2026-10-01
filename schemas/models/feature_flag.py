"""
Feature flag document model.

Maps to the `feature_flags` MongoDB collection. One document per registered
flag. The document IS the rollout state — there is no admin API; engineers
edit Mongo directly via mongosh and the running app picks up changes within
the cache TTL window (~60s).

The `enabled` boolean is a coarse on/off; `rollout_type` decides the audience
when enabled. ``RolloutType.OFF`` is a separate kill-switch independent of
``enabled`` so a flag can be paused without rewriting its other fields.
"""

from __future__ import annotations

from datetime import datetime

from bson import ObjectId
from pydantic import Field, field_validator

from schemas.enums.rollout_type import RolloutType
from schemas.models.base import MongoBaseModel, PyObjectId


class FeatureFlagDoc(MongoBaseModel):
    """Document model for the `feature_flags` collection."""

    name: str
    enabled: bool = False
    rollout_type: RolloutType = RolloutType.OFF

    # ALLOWLIST fields — match either user_id or lowercased email.
    allowlist_user_ids: list[PyObjectId] = Field(default_factory=list)
    allowlist_emails: list[str] = Field(default_factory=list)

    # PERCENTAGE field — 0-100 inclusive. Stable hash determines bucket.
    percentage: int = 0

    # HEX_DIGIT field — list of single hex chars (0-f). Each digit ≈ 6.25% of
    # users. Stable hash places each user in exactly one bucket per flag.
    enabled_digits: list[str] = Field(default_factory=list)

    # Free-form note for the engineer editing Mongo. Not surfaced to users.
    description: str = ""

    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("name")
    @classmethod
    def _name_non_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("flag name must not be empty")
        return v

    @field_validator("percentage")
    @classmethod
    def _percentage_in_range(cls, v: int) -> int:
        if not 0 <= v <= 100:
            raise ValueError(f"percentage must be 0-100, got {v}")
        return v

    @field_validator("enabled_digits")
    @classmethod
    def _digits_are_single_hex(cls, v: list[str]) -> list[str]:
        if len(v) > 16:
            raise ValueError(f"enabled_digits has at most 16 entries, got {len(v)}")
        normalised: list[str] = []
        for d in v:
            if not isinstance(d, str) or len(d) != 1 or d not in "0123456789abcdef":
                raise ValueError(
                    f"enabled_digits must be single lowercase hex chars 0-f, got {d!r}"
                )
            normalised.append(d)
        # De-duplicate while preserving order — the digits list is the rollout
        # state; duplicates would mislead the human reading Mongo.
        seen: set[str] = set()
        deduped = [d for d in normalised if not (d in seen or seen.add(d))]
        return deduped

    @field_validator("allowlist_emails")
    @classmethod
    def _normalise_emails(cls, v: list[str]) -> list[str]:
        # Lowercase for case-insensitive comparison in is_enabled.
        return [e.strip().lower() for e in v if e.strip()]

    def is_user_in_allowlist(self, user_id: ObjectId, email: str | None) -> bool:
        """Return True when the user matches by id or normalised email.

        Kept on the model so the service layer doesn't reach into the doc's
        internal field shape.
        """
        if user_id in self.allowlist_user_ids:
            return True
        return bool(email and email.lower() in self.allowlist_emails)
