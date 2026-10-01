"""
Entitlement audit event: append-only record of every subscription
transition, override write, and lifecycle action for a user.

The count of a user's entitlement-changing events is their entitlement
version: every effective write appends exactly one, so the version changes
on grants and revokes alike, and never when a write changed nothing.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from schemas.models.base import MongoBaseModel, PyObjectId


class EntitlementEventKind(str, Enum):
    SUBSCRIPTION_CHANGED = "subscription_changed"
    OVERRIDE_GRANTED = "override_granted"
    OVERRIDE_REVOKED = "override_revoked"
    LAPSED = "lapsed"
    GRACE_STARTED = "grace_started"
    REMINDER_SENT = "reminder_sent"


class EntitlementEventDoc(MongoBaseModel):
    user_id: PyObjectId
    kind: EntitlementEventKind
    actor: str
    reason: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    at: datetime
