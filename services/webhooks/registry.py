"""EVENT_REGISTRY — the webhook event catalog as code.

One registry drives five things that therefore can never drift: publish-time
payload validation, the public catalog endpoint, docs sample payloads, test
sends, and wildcard expansion.

Catalog governance (the forever-contract rules):
- An event is a fact someone would act on without having been there.
- Payload = full resource snapshot + event context. Additive changes only;
  remove/rename = envelope version bump.
- Events split by CAUSER, not by field: actor edits (owner, API key,
  connected app) ride ``link.updated`` and its ``changes`` map — status
  included; system-discovered facts get named events (``link.expired``
  today; ``link.blocked`` when the safety framework ships a producer).
  ``link.status_changed`` was cut for violating this: it only ever fired
  for owner edits and double-fired alongside ``link.updated``.
- ``link.clicked`` fires for every TRACKED click including bots (``is_bot``
  rides the payload). Blocked bots on ``block_bots`` links produce no click
  and therefore no event — deliberate: a dedicated ``bot.detected`` event
  was considered and cut (the payload flag covers the tracked case, and
  blocked-bot noise isn't worth a forever-contract).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Any

from pydantic import BaseModel, ConfigDict

from errors import ValidationError

_SAMPLE_LINK_ID = "665f1f77bcf86cd799439011"
_SAMPLE_OWNER_ID = "507f1f77bcf86cd799439011"


# ── Payload models ────────────────────────────────────────────────────────────
# extra="allow" on the base: payloads are additive-forever (consumers must
# tolerate new fields), so validation pins required shape without freezing it.


class _PayloadBase(BaseModel):
    model_config = ConfigDict(extra="allow")


class LinkSnapshot(_PayloadBase):
    """The public snapshot of a link — field names track UrlResponse.
    Feature fields are null when unset on the DOCUMENT (no entitlement
    lookups on the event path — flags gate writes, not payloads)."""

    link_id: str
    alias: str
    domain: str
    short_url: str
    long_url: str
    status: str
    password_protected: bool = False
    block_bots: bool = False
    max_clicks: int | None = None
    expires_at: str | None = None
    geo_rules: dict[str, str] | None = None
    meta_tags: dict[str, Any] | None = None
    total_clicks: int = 0
    created_at: str


class LinkLifecyclePayload(_PayloadBase):
    link: LinkSnapshot


class LinkUpdatedPayload(_PayloadBase):
    link: LinkSnapshot
    changes: dict[str, dict[str, Any]]  # field -> {"old": …, "new": …}


class LinkClickedPayload(_PayloadBase):
    link_id: str
    alias: str
    domain: str
    short_url: str
    clicked_at: str
    country: str | None = None
    city: str | None = None
    browser: str | None = None
    os: str | None = None
    device: str | None = None
    referrer: str | None = None
    long_url: str | None = None
    utm: dict[str, str | None] | None = None
    is_bot: bool = False
    bot_name: str | None = None
    total_clicks: int | None = None


class LinkExpiredPayload(_PayloadBase):
    link: LinkSnapshot
    reason: str  # "max_clicks_reached" | "time_expired"


class LinkBlockedPayload(_PayloadBase):
    link: LinkSnapshot
    reason: str  # human-readable safety verdict reason


class WebhookTestPayload(_PayloadBase):
    message: str


# ── Samples ───────────────────────────────────────────────────────────────────


def _sample_link() -> dict[str, Any]:
    return {
        "link_id": _SAMPLE_LINK_ID,
        "alias": "summer-drop",
        "domain": "spoo.me",
        "short_url": "https://spoo.me/summer-drop",
        "long_url": "https://example.com/campaign",
        "status": "ACTIVE",
        "password_protected": False,
        "block_bots": False,
        "max_clicks": None,
        "expires_at": None,
        "geo_rules": None,
        "meta_tags": None,
        "total_clicks": 4102,
        "created_at": "2026-07-01T09:00:00+00:00",
    }


# ── Registry ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EventTypeSpec:
    name: str
    category: str  # "link" today; new categories get their own wildcard for free
    description: str
    frequency: str  # "high" | "low"
    payload_model: type[BaseModel]
    sample: Callable[[], dict[str, Any]]


EVENT_REGISTRY: dict[str, EventTypeSpec] = {
    spec.name: spec
    for spec in (
        EventTypeSpec(
            name="link.created",
            category="link",
            description="A short link was created.",
            frequency="low",
            payload_model=LinkLifecyclePayload,
            sample=lambda: {"link": _sample_link()},
        ),
        EventTypeSpec(
            name="link.updated",
            category="link",
            description=(
                "A short link was edited (any field, status included). "
                "`changes` maps each edited field to its old and new values."
            ),
            frequency="low",
            payload_model=LinkUpdatedPayload,
            sample=lambda: {
                "link": _sample_link(),
                "changes": {
                    "long_url": {
                        "old": "https://example.com/old",
                        "new": "https://example.com/campaign",
                    }
                },
            },
        ),
        EventTypeSpec(
            name="link.deleted",
            category="link",
            description="A short link was deleted.",
            frequency="low",
            payload_model=LinkLifecyclePayload,
            sample=lambda: {"link": _sample_link()},
        ),
        EventTypeSpec(
            name="link.clicked",
            category="link",
            description=(
                "A tracked click was recorded (bots included — check "
                "`is_bot`). Edge-served clicks are not tracked."
            ),
            frequency="high",
            payload_model=LinkClickedPayload,
            sample=lambda: {
                "link_id": _SAMPLE_LINK_ID,
                "alias": "summer-drop",
                "domain": "spoo.me",
                "short_url": "https://spoo.me/summer-drop",
                "long_url": "https://example.com/campaign",
                "clicked_at": "2026-07-22T14:03:10+00:00",
                "country": "IN",
                "city": "Mumbai",
                "browser": "Chrome",
                "os": "Android",
                "device": "mobile",
                "user_agent": "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36",
                "referrer": "https://x.com/",
                "utm": {"source": "newsletter", "medium": "email", "campaign": None},
                "is_bot": False,
                "bot_name": None,
                "total_clicks": 4102,
            },
        ),
        EventTypeSpec(
            name="link.expired",
            category="link",
            description=(
                "A link expired (max clicks reached, or its expiry time was "
                "discovered to have passed)."
            ),
            frequency="low",
            payload_model=LinkExpiredPayload,
            sample=lambda: {
                "link": {**_sample_link(), "status": "EXPIRED", "max_clicks": 5000},
                "reason": "max_clicks_reached",
            },
        ),
        EventTypeSpec(
            name="link.blocked",
            category="link",
            description=(
                "A link was blocked by the safety system because its "
                "destination was judged malicious."
            ),
            frequency="low",
            payload_model=LinkBlockedPayload,
            sample=lambda: {
                "link": {**_sample_link(), "status": "BLOCKED"},
                "reason": "destination matched a known phishing source",
            },
        ),
    )
}

# webhook.test is sendable (test sends may use any type above OR this one)
# but not subscribable — it is delivered to the endpoint under test directly.
TEST_EVENT_TYPE = "webhook.test"
TEST_EVENT_SPEC = EventTypeSpec(
    name=TEST_EVENT_TYPE,
    category="webhook",
    description="A test ping sent from the dashboard or API.",
    frequency="low",
    payload_model=WebhookTestPayload,
    sample=lambda: {"message": "If you can read this, your endpoint works."},
)

_WILDCARD_ALL = "*"


def valid_patterns() -> frozenset[str]:
    """Every string accepted in an endpoint's ``events`` list."""
    names = set(EVENT_REGISTRY)
    categories = {f"{spec.category}.*" for spec in EVENT_REGISTRY.values()}
    return frozenset(names | categories | {_WILDCARD_ALL})


def expand(patterns: list[str]) -> frozenset[str]:
    """Expand subscription patterns to concrete event names.

    Expansion happens at MATCH time against the current registry (never
    persisted expanded), so ``link.*`` subscribers pick up future link
    events automatically.
    """
    out: set[str] = set()
    for pattern in patterns:
        if pattern == _WILDCARD_ALL:
            return frozenset(EVENT_REGISTRY)
        if pattern.endswith(".*"):
            category = pattern[:-2]
            out.update(
                name
                for name, spec in EVENT_REGISTRY.items()
                if spec.category == category
            )
        elif pattern in EVENT_REGISTRY:
            out.add(pattern)
    return frozenset(out)


def validate_patterns(patterns: list[str]) -> None:
    """Raise ValidationError (with a typo suggestion) on unknown patterns."""
    valid = valid_patterns()
    for pattern in patterns:
        if pattern in valid:
            continue
        suggestion = get_close_matches(pattern, sorted(valid), n=1, cutoff=0.6)
        hint = f" Did you mean '{suggestion[0]}'?" if suggestion else ""
        raise ValidationError(f"Unknown event type '{pattern}'.{hint}")


def validate_payload(event_type: str, payload: dict[str, Any]) -> None:
    """Publish-time payload validation against the registry model."""
    spec = (
        TEST_EVENT_SPEC
        if event_type == TEST_EVENT_TYPE
        else EVENT_REGISTRY.get(event_type)
    )
    if spec is None:
        raise ValidationError(f"Unknown event type '{event_type}'")
    spec.payload_model.model_validate(payload)
