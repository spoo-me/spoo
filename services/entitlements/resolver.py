"""
Pure resolution: plan defaults with overrides applied on top.

No I/O. ``resolve`` is the one rule (override beats plan default beats
system default) and ``Resolved`` is the shape every reader consumes and
the cache stores.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from pydantic import BaseModel, Field

from infrastructure.logging import get_logger
from schemas.models.entitlement_override import EntitlementOverrideDoc
from schemas.models.subscription import SubscriptionStatus
from services.features.catalog import FEATURES, UNLIMITED, Kind, Plan, plan_defaults

log = get_logger(__name__)


class Resolved(BaseModel):
    plan: Plan
    status: SubscriptionStatus | None = None
    until: datetime | None = None
    founding: bool = False
    # True only for an active recurring term; prepaid terms end, they do not renew.
    renews: bool = False
    values: dict[str, bool | int]
    version: int
    # True when this answer came from a fallback because the stores were
    # unreachable. Never cached, never sent as a version header.
    degraded: bool = Field(default=False, exclude=True)

    def has(self, key: str) -> bool:
        return bool(self.values.get(key, False))

    def limit(self, key: str) -> int:
        value = self.values.get(key, 0)
        return int(value) if not isinstance(value, bool) else 0

    def is_unlimited(self, key: str) -> bool:
        return self.limit(key) == UNLIMITED

    def within_limit(self, key: str, current: int) -> bool:
        return self.is_unlimited(key) or current < self.limit(key)


def resolve(
    plan: Plan, overrides: Iterable[EntitlementOverrideDoc] = ()
) -> dict[str, bool | int]:
    values = plan_defaults(plan)
    for o in overrides:
        feature = FEATURES.get(o.key)
        if feature is None:
            log.warning("entitlement_override_unknown_key", key=o.key)
            continue
        values[o.key] = bool(o.value) if feature.kind is Kind.BOOL else int(o.value)
    return values


def for_plan(plan: Plan, *, version: int = 0) -> Resolved:
    return Resolved(plan=plan, values=plan_defaults(plan), version=version)


ANONYMOUS: Resolved = for_plan(Plan.ANONYMOUS)
