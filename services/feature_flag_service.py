"""
Feature flag service: the deployment-level evaluator.

Answers one question per catalog feature: is its rollout on in this
deployment for this user. ``FEATURES[key].rollout`` names the
``feature_flags`` document; ``None`` means the rollout is finished and the
answer is always True. Default-deny on every error path:

  - Key not in the catalog          → False
  - Doc missing                     → False (forces explicit registration)
  - Doc.enabled = False             → False
  - rollout_type = OFF              → False (kill switch)
  - user is None on a non-EVERYONE  → False
  - Cache + repo failure            → False (with logged error)

Plans are not this service's business. ``states_for`` joins its answer with
the entitlement resolver's: flag off is hidden, flag on and entitled is
enabled, flag on and not entitled is locked.

The service consults a read-through Redis cache (60s positive TTL, 30s
negative TTL). Flag mutations happen via direct mongosh edits with no app
event, so changes propagate within the cache TTL window.

Stable hashing uses ``blake2b(salt=name + user_id)`` so the same user gets
different positions in different flags' rollouts, enabling independent
percentage and hex-digit gates per feature.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING

from bson import ObjectId

from errors import ForbiddenError, NotFoundError
from infrastructure.cache.feature_flag_cache import (
    NEGATIVE_MISS,
    FeatureFlagCache,
)
from infrastructure.logging import get_logger
from repositories.feature_flag_repository import FeatureFlagRepository
from schemas.enums.feature_state import FeatureState
from schemas.enums.rollout_type import RolloutType
from schemas.models.feature_flag import FeatureFlagDoc
from services.features.catalog import FEATURES, bool_features

if TYPE_CHECKING:
    # CurrentUser lives in dependencies.auth which imports back through
    # dependencies/__init__.py — avoid the circular import by importing the
    # type only for type-checking. ``__future__ annotations`` makes the
    # runtime annotation a string so the import is never resolved at runtime.
    from dependencies.auth import CurrentUser
    from services.entitlements.resolver import Resolved

log = get_logger(__name__)

# Catalog keys referenced by routes. Code names features through these,
# never through bare string literals at call sites.
CUSTOM_DOMAINS_FLAG = "custom_domains"
GEO_TARGETING_FLAG = "geo_targeting"
META_TAGS_FLAG = "custom_meta_tags"
AB_TESTING_FLAG = "ab_variants"
WEBHOOKS_FLAG = "webhooks"
EXPIRED_FALLBACK_FLAG = "expired_fallback"
LINK_SCHEDULING_FLAG = "link_scheduling"

# Every BOOL feature in the catalog is exposed to clients via
# GET /api/v1/me/entitlements so frontends can decide what to render.
EXPOSED_FEATURES: tuple[str, ...] = tuple(f.key for f in bool_features())


def _stable_hash(user_id: ObjectId, salt: str) -> int:
    """Return 0-99 deterministically per ``(user_id, salt)``.

    Used by ``RolloutType.PERCENTAGE``. The salt is the flag name so the same
    user has independent positions across different flags' rollouts.
    """
    h = hashlib.blake2b(f"{salt}:{user_id}".encode(), digest_size=4).digest()
    return int.from_bytes(h, "big") % 100


def _digit_bucket(user_id: ObjectId, salt: str) -> str:
    """Return a single hex digit (0-f) deterministically per ``(user_id, salt)``.

    Used by ``RolloutType.HEX_DIGIT``. 16 buckets, each ≈6.25% of users.
    """
    h = hashlib.blake2b(f"{salt}:{user_id}".encode(), digest_size=1).hexdigest()
    return h[0]


class FeatureFlagService:
    def __init__(
        self,
        repo: FeatureFlagRepository,
        cache: FeatureFlagCache,
    ) -> None:
        self._repo = repo
        self._cache = cache

    async def is_enabled(self, key: str, user: CurrentUser | None) -> bool:
        """Return whether the catalog feature ``key`` is rolled out to ``user``.

        Default-deny on any error or unknown key.
        """
        feature = FEATURES.get(key)
        if feature is None:
            log.warning("feature_flag_unknown_key", key=key)
            return False
        if feature.rollout is None:
            return True

        flag = await self._lookup(feature.rollout)
        if flag is None or not flag.enabled:
            return False

        rollout = flag.rollout_type

        if rollout == RolloutType.OFF:
            return False
        if rollout == RolloutType.EVERYONE:
            return True

        # All non-EVERYONE rollouts require an authenticated user — anonymous
        # callers get default-deny.
        if user is None:
            return False

        if rollout == RolloutType.ALLOWLIST:
            return flag.is_user_in_allowlist(user.user_id, _email_of(user))

        if rollout == RolloutType.PERCENTAGE:
            return _stable_hash(user.user_id, salt=flag.name) < flag.percentage

        if rollout == RolloutType.HEX_DIGIT:
            return _digit_bucket(user.user_id, salt=flag.name) in flag.enabled_digits

        # Unreachable today — Pydantic validates rollout_type against the
        # RolloutType enum. Kept as default-deny if the field is ever widened.
        log.warning(
            "feature_flag_unknown_rollout", name=flag.name, rollout=str(rollout)
        )
        return False

    async def states_for(
        self, user: CurrentUser | None, entitlements: Resolved
    ) -> dict[str, FeatureState]:
        """Per-user state of every client-exposed feature.

        Flag off → HIDDEN (the feature does not exist here yet). Flag on and
        entitled → ENABLED. Flag on and not entitled → LOCKED, so the client
        renders the upsell instead of nothing.

        Inherits ``is_enabled``'s default-deny: unregistered flags, cache
        and repo failures all read as HIDDEN.
        """
        answers = await asyncio.gather(
            *(self.is_enabled(key, user) for key in EXPOSED_FEATURES)
        )
        states: dict[str, FeatureState] = {}
        for key, rolled_out in zip(EXPOSED_FEATURES, answers, strict=True):
            if not rolled_out:
                states[key] = FeatureState.HIDDEN
            elif entitlements.has(key):
                states[key] = FeatureState.ENABLED
            else:
                states[key] = FeatureState.LOCKED
        return states

    async def require(
        self, key: str, user: CurrentUser | None, *, hide: bool = False
    ) -> None:
        """Raise unless ``key`` is rolled out to ``user``.

        403 by default — the right signal for flag-gated FIELDS on shared
        endpoints, which appear in public OpenAPI docs and can't be
        concealed. Pass ``hide=True`` for whole-endpoint features whose
        existence is itself gated (the custom-domains pattern): 404, so
        non-allowlisted callers can't tell the feature exists.
        """
        if await self.is_enabled(key, user):
            return
        if hide:
            raise NotFoundError("not found")
        feature = key.replace("_", " ").capitalize()
        raise ForbiddenError(f"{feature} is not enabled for this account")

    async def _lookup(self, name: str) -> FeatureFlagDoc | None:
        """Fetch a flag through cache → repo, returning None for unregistered."""
        try:
            cached = await self._cache.get(name)
        except Exception as e:
            log.warning("feature_flag_cache_lookup_error", name=name, error=str(e))
            cached = None

        if cached is NEGATIVE_MISS:
            return None
        if isinstance(cached, FeatureFlagDoc):
            return cached

        try:
            doc = await self._repo.find_by_name(name)
        except Exception as e:
            log.error("feature_flag_repo_lookup_error", name=name, error=str(e))
            return None

        if doc is None:
            await self._cache.set_negative(name)
            return None
        await self._cache.set(name, doc)
        return doc


def _email_of(user: CurrentUser) -> str | None:
    """Extract the user's lowercased email for allowlist matching.

    ``CurrentUser.email`` is populated on both auth paths: from the "email"
    JWT claim and from the owning ``UserDoc`` on the API-key path. It is
    ``None`` only for access tokens minted before the claim existed (fixed
    on the next refresh) — those users still match by user_id. ``getattr``
    keeps this tolerant of CurrentUser-shaped stubs without the field.
    """
    return getattr(user, "email", None)
