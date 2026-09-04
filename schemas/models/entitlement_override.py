"""
Entitlement override document: one per (user, feature key).

The single mechanism for comps, beta access, abuse throttles, and custom
limits. Values are absolute, never deltas: an override replaces the plan
default for that key until it expires or is revoked.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from schemas.models.base import MongoBaseModel, PyObjectId
from shared.datetime_utils import as_aware_utc


class OverrideKind(str, Enum):
    COMP = "comp"
    BETA = "beta"
    ABUSE = "abuse"
    CUSTOM = "custom"


class EntitlementOverrideDoc(MongoBaseModel):
    user_id: PyObjectId
    key: str
    value: bool | int
    kind: OverrideKind
    reason: str
    granted_by: str
    expires_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def is_expired(self, now: datetime) -> bool:
        exp = as_aware_utc(self.expires_at)
        return exp is not None and exp <= now
