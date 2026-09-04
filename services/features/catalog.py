"""
Feature catalog: every gated capability and limit, declared exactly once.

Two evaluators read this table and nothing else declares a feature:

- the flag service answers "is this feature rolled out in this deployment"
  from ``rollout`` (the ``feature_flags`` document name; ``None`` means the
  rollout is finished and the feature is always on)
- the entitlement resolver answers "does this principal hold it" from
  ``plans``

A finished rollout is retired by setting ``rollout=None``, never by removing
the entry, so a flag reaching 100% can never delete a paid gate. Limits are
``INT`` and never have a rollout; ``-1`` means unlimited.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from schemas.enums.plan import Plan


class Kind(str, Enum):
    BOOL = "bool"
    INT = "int"


class Lapse(str, Enum):
    """What existing data does when its owner loses a BOOL feature."""

    DISABLE = "disable"
    KEEP = "keep"
    READONLY = "readonly"


class OverLimit(str, Enum):
    """What happens to existing items when an INT limit shrinks below them.

    ``None`` on a limit means nothing is paused (windows, rates, batch sizes).
    """

    PAUSE_NEWEST = "pause_newest"
    PAUSE_ALL = "pause_all"


UNLIMITED = -1


@dataclass(frozen=True)
class Feature:
    key: str
    kind: Kind
    rollout: str | None
    plans: dict[Plan, bool | int]
    lapse: Lapse | None = None
    over_limit: OverLimit | None = None


def _bool(
    key: str, *, rollout: str | None, free: bool, lapse: Lapse, anonymous: bool = False
) -> Feature:
    return Feature(
        key=key,
        kind=Kind.BOOL,
        rollout=rollout,
        plans={
            Plan.ANONYMOUS: anonymous,
            Plan.FREE: free,
            Plan.PRO: True,
            Plan.SELFHOST: True,
        },
        lapse=lapse,
    )


def _int(
    key: str, *, anonymous: int, free: int, pro: int, over_limit: OverLimit | None
) -> Feature:
    return Feature(
        key=key,
        kind=Kind.INT,
        rollout=None,
        plans={
            Plan.ANONYMOUS: anonymous,
            Plan.FREE: free,
            Plan.PRO: pro,
            Plan.SELFHOST: UNLIMITED,
        },
        over_limit=over_limit,
    )


_ENTRIES: tuple[Feature, ...] = (
    _bool("geo_targeting", rollout="geo_targeting", free=False, lapse=Lapse.DISABLE),
    # Edge KV keeps an already-written card for up to its TTL after a lapse.
    _bool(
        "custom_meta_tags", rollout="custom_meta_tags", free=False, lapse=Lapse.DISABLE
    ),
    _bool("ab_variants", rollout="ab_testing", free=False, lapse=Lapse.DISABLE),
    _bool(
        "expired_fallback", rollout="expired_fallback", free=False, lapse=Lapse.DISABLE
    ),
    _bool("link_scheduling", rollout="link_scheduling", free=False, lapse=Lapse.KEEP),
    _bool("custom_domains", rollout="custom_domains", free=False, lapse=Lapse.DISABLE),
    _bool("domain_polish", rollout="domain_polish", free=False, lapse=Lapse.DISABLE),
    _bool("qr_custom_logo", rollout="qr_custom_logo", free=False, lapse=Lapse.KEEP),
    _bool(
        "live_click_stream",
        rollout="live_click_stream",
        free=False,
        lapse=Lapse.DISABLE,
    ),
    _bool(
        "analytics_extra_views",
        rollout="analytics_extra_views",
        free=False,
        lapse=Lapse.DISABLE,
    ),
    _bool(
        "viral_full_tracking",
        rollout="viral_full_tracking",
        free=False,
        lapse=Lapse.DISABLE,
    ),
    # Rollout-only: every plan has webhooks, the endpoint count is the limit.
    _bool("webhooks", rollout="webhooks", free=True, lapse=Lapse.KEEP),
    _int(
        "custom_domains_max",
        anonymous=0,
        free=0,
        pro=5,
        over_limit=OverLimit.PAUSE_NEWEST,
    ),
    _int(
        "webhook_endpoints_max",
        anonymous=0,
        free=1,
        pro=10,
        over_limit=OverLimit.PAUSE_NEWEST,
    ),
    _int(
        "api_keys_max", anonymous=0, free=20, pro=20, over_limit=OverLimit.PAUSE_NEWEST
    ),
    _int(
        "analytics_window_days",
        anonymous=90,
        free=90,
        pro=730,
        over_limit=None,
    ),
    _int("api_rate_multiplier", anonymous=1, free=1, pro=5, over_limit=None),
    _int("bulk_batch_max", anonymous=0, free=100, pro=1000, over_limit=None),
)

FEATURES: dict[str, Feature] = {f.key: f for f in _ENTRIES}


def bool_features() -> tuple[Feature, ...]:
    return tuple(f for f in FEATURES.values() if f.kind is Kind.BOOL)


def int_features() -> tuple[Feature, ...]:
    return tuple(f for f in FEATURES.values() if f.kind is Kind.INT)


def plan_defaults(plan: Plan) -> dict[str, bool | int]:
    return {key: f.plans[plan] for key, f in FEATURES.items()}


def validate_override(key: str, value: bool | int) -> None:
    """Reject an override the resolver would ignore or misread."""
    feature = FEATURES.get(key)
    if feature is None:
        raise ValueError(f"unknown entitlement {key!r}")
    if feature.kind is Kind.BOOL and not isinstance(value, bool):
        raise ValueError(f"{key} takes a bool, got {value!r}")
    if feature.kind is Kind.INT and (
        isinstance(value, bool) or not isinstance(value, int)
    ):
        raise ValueError(f"{key} takes an int, got {value!r}")
