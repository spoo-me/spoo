"""Unit tests for FeatureFlagService — rollout strategies + caching + default-deny."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from bson import ObjectId

from errors import ForbiddenError, NotFoundError
from infrastructure.cache.feature_flag_cache import NEGATIVE_MISS
from schemas.enums.rollout_type import RolloutType
from schemas.models.feature_flag import FeatureFlagDoc
from services.feature_flag_service import (
    FEATURES,
    FeatureFlagService,
    _digit_bucket,
    _stable_hash,
)
from services.features.catalog import Feature, Kind, Lapse, Plan

USER_A = ObjectId("aaaaaaaaaaaaaaaaaaaaaaaa")
USER_B = ObjectId("bbbbbbbbbbbbbbbbbbbbbbbb")
USER_C = ObjectId("cccccccccccccccccccccccc")


@pytest.fixture(autouse=True)
def _test_flag_in_catalog(monkeypatch):
    """Register ``test_flag`` as a catalog feature whose rollout is the
    ``test_flag`` document, so the rollout strategies can be exercised
    without touching a real feature's entry."""
    monkeypatch.setitem(
        FEATURES,
        "test_flag",
        Feature(
            key="test_flag",
            kind=Kind.BOOL,
            rollout="test_flag",
            plans={p: True for p in Plan},
            lapse=Lapse.KEEP,
        ),
    )


def _flag(**overrides) -> FeatureFlagDoc:
    base = {
        "name": "test_flag",
        "enabled": True,
        "rollout_type": RolloutType.OFF,
        "allowlist_user_ids": [],
        "allowlist_emails": [],
        "percentage": 0,
        "enabled_digits": [],
        "description": "",
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    base.update(overrides)
    return FeatureFlagDoc.model_validate(base)


def _user(user_id: ObjectId = USER_A, email: str | None = None):
    """Build a stub CurrentUser-shaped object via SimpleNamespace.

    The real ``CurrentUser`` carries ``user_id`` always; ``email`` is
    accessed defensively via ``getattr``.
    """
    from types import SimpleNamespace

    attrs = {
        "user_id": user_id,
        "email_verified": True,
        "amr": "pwd",
        "api_key_doc": None,
    }
    if email is not None:
        attrs["email"] = email
    return SimpleNamespace(**attrs)


def make_service(flag: FeatureFlagDoc | None = None, cache_returns=None):
    """Build a FeatureFlagService with mocked repo + cache.

    By default the cache misses (returns ``None``) and the repo returns
    ``flag``. Override with ``cache_returns`` to test cache hits.
    """
    repo = AsyncMock()
    repo.find_by_name = AsyncMock(return_value=flag)
    cache = AsyncMock()
    cache.get = AsyncMock(return_value=cache_returns)
    cache.set = AsyncMock()
    cache.set_negative = AsyncMock()
    return FeatureFlagService(repo, cache), repo, cache


# ── Default-deny paths ───────────────────────────────────────────────────────


class TestDefaultDeny:
    @pytest.mark.asyncio
    async def test_unregistered_flag_returns_false(self):
        service, _repo, cache = make_service(flag=None)
        assert await service.is_enabled("test_flag", _user()) is False
        cache.set_negative.assert_awaited_once_with("test_flag")

    @pytest.mark.asyncio
    async def test_key_outside_catalog_returns_false_without_lookup(self):
        service, repo, cache = make_service(flag=_flag(rollout_type="everyone"))
        assert await service.is_enabled("missing", _user()) is False
        repo.find_by_name.assert_not_awaited()
        cache.get.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_finished_rollout_is_always_on(self, monkeypatch):
        monkeypatch.setitem(
            FEATURES,
            "done",
            Feature(
                key="done",
                kind=Kind.BOOL,
                rollout=None,
                plans={p: True for p in Plan},
                lapse=Lapse.KEEP,
            ),
        )
        service, repo, _ = make_service(flag=None)
        assert await service.is_enabled("done", None) is True
        repo.find_by_name.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_disabled_flag_returns_false(self):
        flag = _flag(enabled=False, rollout_type=RolloutType.EVERYONE)
        service, _, _ = make_service(flag=flag)
        assert await service.is_enabled("test_flag", _user()) is False

    @pytest.mark.asyncio
    async def test_off_rollout_returns_false_even_if_enabled(self):
        flag = _flag(enabled=True, rollout_type=RolloutType.OFF)
        service, _, _ = make_service(flag=flag)
        assert await service.is_enabled("test_flag", _user()) is False

    @pytest.mark.asyncio
    async def test_anonymous_user_blocked_for_non_everyone(self):
        flag = _flag(rollout_type=RolloutType.PERCENTAGE, percentage=100)
        service, _, _ = make_service(flag=flag)
        assert await service.is_enabled("test_flag", None) is False

    @pytest.mark.asyncio
    async def test_repo_error_returns_false(self):
        repo = AsyncMock()
        repo.find_by_name = AsyncMock(side_effect=RuntimeError("db down"))
        cache = AsyncMock()
        cache.get = AsyncMock(return_value=None)
        service = FeatureFlagService(repo, cache)
        assert await service.is_enabled("test_flag", _user()) is False


# ── EVERYONE ─────────────────────────────────────────────────────────────────


class TestRolloutEveryone:
    @pytest.mark.asyncio
    async def test_authenticated_user_allowed(self):
        flag = _flag(rollout_type=RolloutType.EVERYONE)
        service, _, _ = make_service(flag=flag)
        assert await service.is_enabled("test_flag", _user()) is True

    @pytest.mark.asyncio
    async def test_anonymous_user_allowed(self):
        flag = _flag(rollout_type=RolloutType.EVERYONE)
        service, _, _ = make_service(flag=flag)
        # EVERYONE bypasses the anonymous-user gate.
        assert await service.is_enabled("test_flag", None) is True


# ── ALLOWLIST ────────────────────────────────────────────────────────────────


class TestRolloutAllowlist:
    @pytest.mark.asyncio
    async def test_user_id_match(self):
        flag = _flag(rollout_type=RolloutType.ALLOWLIST, allowlist_user_ids=[USER_A])
        service, _, _ = make_service(flag=flag)
        assert await service.is_enabled("test_flag", _user(USER_A)) is True
        assert await service.is_enabled("test_flag", _user(USER_B)) is False

    @pytest.mark.asyncio
    async def test_owner_by_id_matches_the_id_allowlist_only(self):
        flag = _flag(
            rollout_type=RolloutType.ALLOWLIST,
            allowlist_user_ids=[USER_A],
            allowlist_emails=["bob@example.com"],
        )
        service, _, _ = make_service(flag=flag)
        assert await service.is_enabled_for_owner("test_flag", USER_A) is True
        assert await service.is_enabled_for_owner("test_flag", USER_B) is False

    @pytest.mark.asyncio
    async def test_email_match_case_insensitive(self):
        flag = _flag(
            rollout_type=RolloutType.ALLOWLIST,
            allowlist_emails=["alice@example.com"],
        )
        service, _, _ = make_service(flag=flag)
        assert (
            await service.is_enabled("test_flag", _user(email="ALICE@example.com"))
            is True
        )
        assert (
            await service.is_enabled("test_flag", _user(email="bob@example.com"))
            is False
        )

    @pytest.mark.asyncio
    async def test_no_email_attribute_falls_back_to_user_id(self):
        # Stubs without an email attribute — service must not crash.
        flag = _flag(rollout_type=RolloutType.ALLOWLIST, allowlist_user_ids=[USER_A])
        service, _, _ = make_service(flag=flag)
        # _user() with no email omits the attribute; getattr returns None.
        assert await service.is_enabled("test_flag", _user(USER_A)) is True

    @pytest.mark.asyncio
    async def test_email_only_allowlist_denies_stub_without_email_attribute(self):
        # Pins _email_of's getattr fallback: a user object with NO email
        # attribute at all (not CurrentUser with email=None) against an
        # email-ONLY allowlist → graceful deny, no AttributeError.
        flag = _flag(
            rollout_type=RolloutType.ALLOWLIST,
            allowlist_emails=["alice@example.com"],
        )
        service, _, _ = make_service(flag=flag)
        stub = _user(USER_A)  # SimpleNamespace; email attribute omitted
        assert not hasattr(stub, "email")
        assert await service.is_enabled("test_flag", stub) is False

    @pytest.mark.asyncio
    async def test_current_user_email_field_flows_through(self):
        """CurrentUser.email (JWT "email" claim / UserDoc email) satisfies
        email-only allowlists — the real dataclass, not a stub."""
        from dependencies.auth import CurrentUser

        flag = _flag(
            rollout_type=RolloutType.ALLOWLIST,
            allowlist_emails=["alice@example.com"],
        )
        service, _, _ = make_service(flag=flag)
        alice = CurrentUser(
            user_id=USER_A, email_verified=True, email="alice@example.com"
        )
        bob = CurrentUser(user_id=USER_B, email_verified=True, email="bob@example.com")
        assert await service.is_enabled("test_flag", alice) is True
        assert await service.is_enabled("test_flag", bob) is False

    @pytest.mark.asyncio
    async def test_current_user_mixed_case_email_matches(self):
        # Doc validator lowercases allowlist entries; is_user_in_allowlist
        # lowercases the user's email — any casing on either side matches.
        from dependencies.auth import CurrentUser

        flag = _flag(
            rollout_type=RolloutType.ALLOWLIST,
            allowlist_emails=["Alice@Example.COM"],
        )
        service, _, _ = make_service(flag=flag)
        alice = CurrentUser(
            user_id=USER_A, email_verified=True, email="ALICE@example.com"
        )
        assert await service.is_enabled("test_flag", alice) is True

    @pytest.mark.asyncio
    async def test_current_user_none_email_denied_without_error(self):
        # Old access tokens (pre-"email" claim) resolve to email=None —
        # email-only allowlists deny them, and nothing raises.
        from dependencies.auth import CurrentUser

        flag = _flag(
            rollout_type=RolloutType.ALLOWLIST,
            allowlist_emails=["alice@example.com"],
        )
        service, _, _ = make_service(flag=flag)
        old_token_user = CurrentUser(user_id=USER_A, email_verified=True)
        assert old_token_user.email is None
        assert await service.is_enabled("test_flag", old_token_user) is False

    @pytest.mark.asyncio
    async def test_current_user_none_email_still_matches_by_user_id(self):
        from dependencies.auth import CurrentUser

        flag = _flag(
            rollout_type=RolloutType.ALLOWLIST,
            allowlist_user_ids=[USER_A],
            allowlist_emails=["alice@example.com"],
        )
        service, _, _ = make_service(flag=flag)
        old_token_user = CurrentUser(user_id=USER_A, email_verified=True)
        assert await service.is_enabled("test_flag", old_token_user) is True


# ── PERCENTAGE ───────────────────────────────────────────────────────────────


class TestRolloutPercentage:
    @pytest.mark.asyncio
    async def test_zero_percent_blocks_all(self):
        flag = _flag(rollout_type=RolloutType.PERCENTAGE, percentage=0)
        service, _, _ = make_service(flag=flag)
        for u in (USER_A, USER_B, USER_C):
            assert await service.is_enabled("test_flag", _user(u)) is False

    @pytest.mark.asyncio
    async def test_hundred_percent_allows_all(self):
        flag = _flag(rollout_type=RolloutType.PERCENTAGE, percentage=100)
        service, _, _ = make_service(flag=flag)
        for u in (USER_A, USER_B, USER_C):
            assert await service.is_enabled("test_flag", _user(u)) is True

    @pytest.mark.asyncio
    async def test_stable_for_same_user(self):
        # Same user + same flag name = same answer across calls.
        flag = _flag(rollout_type=RolloutType.PERCENTAGE, percentage=50)
        service, _, _ = make_service(flag=flag)
        first = await service.is_enabled("test_flag", _user(USER_A))
        second = await service.is_enabled("test_flag", _user(USER_A))
        assert first == second


# ── HEX_DIGIT ────────────────────────────────────────────────────────────────


class TestRolloutHexDigit:
    @pytest.mark.asyncio
    async def test_empty_digits_blocks_all(self):
        flag = _flag(rollout_type=RolloutType.HEX_DIGIT, enabled_digits=[])
        service, _, _ = make_service(flag=flag)
        assert await service.is_enabled("test_flag", _user(USER_A)) is False

    @pytest.mark.asyncio
    async def test_full_set_allows_all(self):
        flag = _flag(
            rollout_type=RolloutType.HEX_DIGIT,
            enabled_digits=list("0123456789abcdef"),
        )
        service, _, _ = make_service(flag=flag)
        for u in (USER_A, USER_B, USER_C):
            assert await service.is_enabled("test_flag", _user(u)) is True

    @pytest.mark.asyncio
    async def test_user_in_their_bucket_allowed(self):
        # Compute the actual bucket and confirm the gate respects it.
        bucket = _digit_bucket(USER_A, salt="test_flag")
        flag = _flag(rollout_type=RolloutType.HEX_DIGIT, enabled_digits=[bucket])
        service, _, _ = make_service(flag=flag)
        assert await service.is_enabled("test_flag", _user(USER_A)) is True

    @pytest.mark.asyncio
    async def test_user_outside_their_bucket_blocked(self):
        bucket = _digit_bucket(USER_A, salt="test_flag")
        # Pick a digit guaranteed not to match.
        other = "f" if bucket != "f" else "0"
        flag = _flag(rollout_type=RolloutType.HEX_DIGIT, enabled_digits=[other])
        service, _, _ = make_service(flag=flag)
        assert await service.is_enabled("test_flag", _user(USER_A)) is False


# ── Caching behaviour ────────────────────────────────────────────────────────


class TestCaching:
    @pytest.mark.asyncio
    async def test_cache_hit_skips_repo(self):
        flag = _flag(rollout_type=RolloutType.EVERYONE)
        service, repo, _ = make_service(flag=None, cache_returns=flag)
        result = await service.is_enabled("test_flag", _user())
        assert result is True
        repo.find_by_name.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_negative_cache_hit_skips_repo(self):
        service, repo, _ = make_service(flag=None, cache_returns=NEGATIVE_MISS)
        result = await service.is_enabled("missing", _user())
        assert result is False
        repo.find_by_name.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_repo_hit_populates_cache(self):
        flag = _flag(rollout_type=RolloutType.EVERYONE)
        service, _, cache = make_service(flag=flag)
        await service.is_enabled("test_flag", _user())
        cache.set.assert_awaited_once()
        # First positional arg is the flag name.
        assert cache.set.call_args.args[0] == "test_flag"

    @pytest.mark.asyncio
    async def test_repo_miss_populates_negative_cache(self):
        service, _, cache = make_service(flag=None)
        await service.is_enabled("test_flag", _user())
        cache.set_negative.assert_awaited_once_with("test_flag")


# ── Stable hashing ───────────────────────────────────────────────────────────


class TestStableHash:
    def test_stable_hash_deterministic(self):
        # Same input must yield same output 1000x.
        baseline = _stable_hash(USER_A, "custom_domains")
        for _ in range(1000):
            assert _stable_hash(USER_A, "custom_domains") == baseline

    def test_stable_hash_different_per_salt(self):
        # Same user, different flag names = different positions.
        # Statistical: at least one of these must differ.
        salts = [f"flag_{i}" for i in range(20)]
        positions = {_stable_hash(USER_A, s) for s in salts}
        assert len(positions) > 1, "salt is not influencing the hash"

    def test_stable_hash_in_range(self):
        for u in (USER_A, USER_B, USER_C):
            for salt in ("a", "b", "c"):
                v = _stable_hash(u, salt)
                assert 0 <= v < 100

    def test_digit_bucket_is_single_hex_char(self):
        for u in (USER_A, USER_B, USER_C):
            for salt in ("a", "b", "c"):
                d = _digit_bucket(u, salt)
                assert len(d) == 1
                assert d in "0123456789abcdef"

    def test_digit_bucket_deterministic(self):
        baseline = _digit_bucket(USER_A, "custom_domains")
        for _ in range(1000):
            assert _digit_bucket(USER_A, "custom_domains") == baseline


class TestRequire:
    """require() — the route-facing gate built on is_enabled()."""

    async def test_enabled_flag_returns_silently(self):
        service, _, _ = make_service(
            flag=_flag(enabled=True, rollout_type=RolloutType.EVERYONE)
        )
        await service.require("geo_targeting", None)  # no raise

    async def test_disabled_flag_raises_403_with_readable_feature_name(self):
        service, _, _ = make_service(flag=None)
        with pytest.raises(ForbiddenError, match="Geo targeting is not enabled"):
            await service.require("geo_targeting", None)

    async def test_hide_raises_404_without_leaking_existence(self):
        service, _, _ = make_service(flag=None)
        with pytest.raises(NotFoundError, match="not found"):
            await service.require("custom_domains", None, hide=True)
