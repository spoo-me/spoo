"""
Unit tests for Phase 7 — UrlService.

All external dependencies (repositories, cache) are replaced with AsyncMock.
Tests verify behavior, not implementation details.
"""

from __future__ import annotations

import asyncio
import time as time_module
import typing
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from bson import ObjectId

from app.errors import (
    AppError,
    BlockedUrlError,
    ConflictError,
    ExpiredRedirectError,
    ForbiddenError,
    GoneError,
    NotFoundError,
    NotYetLiveError,
    ValidationError,
)
from app.infrastructure.cache.url_cache import UrlCacheData
from app.schemas.dto.requests.url import UpdateUrlRequest
from app.schemas.models.base import ANONYMOUS_OWNER_ID
from app.schemas.models.url import EmojiUrlDoc, LegacyUrlDoc, UrlStatus, UrlV2Doc

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

USER_OID = ObjectId("aaaaaaaaaaaaaaaaaaaaaaaa")
URL_OID = ObjectId("bbbbbbbbbbbbbbbbbbbbbbbb")
ALIAS = "abc1234"
SYSTEM_DEFAULT_DOMAIN = "spoo.me"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def make_url_v2_doc(
    alias: str = ALIAS,
    url_id: ObjectId = URL_OID,
    owner_id: ObjectId = USER_OID,
    status: str = "ACTIVE",
    block_bots: bool | None = None,
    max_clicks: int | None = None,
    password: str | None = None,
    expire_after: datetime | None = None,
    domain: str | None = None,
    geo_rules: dict | None = None,
    meta_tags: dict | None = None,
    starts_at: datetime | None = None,
    pre_start_url: str | None = None,
    tag_ids: list | None = None,
    ab_variants: list | None = None,
    expired_redirect_url: str | None = None,
) -> UrlV2Doc:
    return UrlV2Doc.from_mongo(
        {
            "_id": url_id,
            "alias": alias,
            "owner_id": owner_id,
            "domain": domain if domain is not None else SYSTEM_DEFAULT_DOMAIN,
            "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
            "creation_ip": "1.2.3.4",
            "long_url": "https://example.com",
            "password": password,
            "block_bots": block_bots,
            "max_clicks": max_clicks,
            "expire_after": expire_after,
            "starts_at": starts_at,
            "pre_start_url": pre_start_url,
            "geo_rules": geo_rules,
            "ab_variants": ab_variants,
            "expired_redirect_url": expired_redirect_url,
            "status": status,
            "private_stats": True,
            "total_clicks": 0,
            "last_click": None,
            "meta_tags": meta_tags,
            "tag_ids": tag_ids or [],
        }
    )


def make_legacy_doc(
    short_code: str = "abcdef",
    url: str = "https://legacy.example.com",
    block_bots: bool = False,
    max_clicks: int | None = None,
    password: str | None = None,
) -> LegacyUrlDoc:
    return LegacyUrlDoc.from_mongo(
        {
            "_id": short_code,
            "url": url,
            "block-bots": block_bots,
            "max-clicks": max_clicks,
            "total-clicks": 0,
            "password": password,
        }
    )


def make_emoji_doc(short_code: str = "🐍🔥💎") -> EmojiUrlDoc:
    return EmojiUrlDoc.from_mongo(
        {
            "_id": short_code,
            "url": "https://emoji.example.com",
            "block-bots": False,
            "total-clicks": 0,
        }
    )


def make_active_cache(
    schema: str = "v2",
    alias: str = ALIAS,
    block_bots: bool = False,
    max_clicks: int | None = None,
    password_hash: str | None = None,
    expiration_time: int | None = None,
    url_status: str = "ACTIVE",
    start_time: int | None = None,
    pre_start_url: str | None = None,
) -> UrlCacheData:
    return UrlCacheData(
        id=str(URL_OID),
        alias=alias,
        long_url="https://example.com",
        block_bots=block_bots,
        password_hash=password_hash,
        expiration_time=expiration_time,
        max_clicks=max_clicks,
        url_status=url_status,
        schema_version=schema,
        owner_id=str(USER_OID),
        domain=SYSTEM_DEFAULT_DOMAIN,
        start_time=start_time,
        pre_start_url=pre_start_url,
    )


def make_repos():
    url_repo = AsyncMock()
    legacy_repo = AsyncMock()
    emoji_repo = AsyncMock()
    blocked_url_repo = AsyncMock()
    url_cache = AsyncMock()
    return url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache


def make_policy(blocked_url_repo):
    """A REAL L0 gate over the mocked pattern repo (ttl=0 so every check
    reads the mock fresh) — blocked/self-link tests keep their semantics."""
    from app.services.safety.policy import UrlPolicyService
    from app.services.safety.providers import BlockedPatternProvider

    return UrlPolicyService(
        [
            BlockedPatternProvider(
                blocked_url_repo, regex_timeout=0.2, patterns_ttl_seconds=0
            )
        ],
        blocked_self_domains=[SYSTEM_DEFAULT_DOMAIN],
    )


def make_service(
    url_repo,
    legacy_repo,
    emoji_repo,
    blocked_url_repo,
    url_cache,
    og_writethrough=None,
    edge_kv=None,
    geo_rules_enabled=True,
    ab_variants_enabled=True,
):
    from app.services.url_service import UrlService

    return UrlService(
        url_repo=url_repo,
        legacy_repo=legacy_repo,
        emoji_repo=emoji_repo,
        blocked_url_repo=blocked_url_repo,
        url_cache=url_cache,
        blocked_self_domains=[SYSTEM_DEFAULT_DOMAIN],
        system_default_domain=SYSTEM_DEFAULT_DOMAIN,
        url_policy=make_policy(blocked_url_repo),
        og_writethrough=og_writethrough,
        edge_kv=edge_kv,
        geo_rules_enabled=geo_rules_enabled,
        ab_variants_enabled=ab_variants_enabled,
    )


# ─────────────────────────────────────────────────────────────────────────────
# TestUrlServiceResolve
# ─────────────────────────────────────────────────────────────────────────────


class TestUrlServiceResolve:
    @pytest.mark.asyncio
    async def test_cache_hit_active_v2_returns_data(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        cached = make_active_cache(schema="v2")
        url_cache.get.return_value = cached

        result, schema = await svc.resolve(ALIAS)

        assert result is cached
        assert schema == "v2"
        url_repo.find_by_alias.assert_not_called()

    @pytest.mark.asyncio
    async def test_cache_hit_blocked_v2_raises_blocked_url_error(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        cached = UrlCacheData(
            id=str(URL_OID),
            alias=ALIAS,
            long_url="",
            block_bots=False,
            password_hash=None,
            expiration_time=None,
            max_clicks=None,
            url_status="BLOCKED",
            schema_version="v2",
            owner_id=str(USER_OID),
        )
        url_cache.get.return_value = cached

        with pytest.raises(BlockedUrlError):
            await svc.resolve(ALIAS)

    @pytest.mark.asyncio
    async def test_blocked_v1_raises_451_error(self):
        """v1 has no status machine — the safety ``blocked`` flag alone
        must surface as the same BlockedUrlError (→451) as a v2 block."""
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        url_cache.get.return_value = None
        doc = make_legacy_doc(short_code="abcdef")
        doc.blocked = True
        legacy_repo.find_by_id.return_value = doc

        with pytest.raises(BlockedUrlError):
            await svc.resolve("abcdef")

    @pytest.mark.asyncio
    async def test_blocked_emoji_raises_451_error(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        url_cache.get.return_value = None
        url_repo.find_by_alias.return_value = None
        doc = make_emoji_doc()
        doc.blocked = True
        emoji_repo.find_by_id.return_value = doc

        with pytest.raises(BlockedUrlError):
            await svc.resolve("🐍🔥💎")

    @pytest.mark.asyncio
    async def test_cache_hit_blocked_v1_raises_451_error(self):
        """The eviction on flip recaches the doc as BLOCKED — the cache-hit
        path must honor a non-v2 BLOCKED status, not gate it on schema."""
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        url_cache.get.return_value = UrlCacheData(
            id="abcdef",
            alias="abcdef",
            long_url="",
            block_bots=False,
            password_hash=None,
            expiration_time=None,
            max_clicks=None,
            url_status="BLOCKED",
            schema_version="v1",
            owner_id=None,
        )
        with pytest.raises(BlockedUrlError):
            await svc.resolve("abcdef")

    @pytest.mark.asyncio
    async def test_cache_hit_expired_v2_raises_gone(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        for status in ("EXPIRED", "INACTIVE"):
            url_cache.get.return_value = UrlCacheData(
                id=str(URL_OID),
                alias=ALIAS,
                long_url="",
                block_bots=False,
                password_hash=None,
                expiration_time=None,
                max_clicks=None,
                url_status=status,
                schema_version="v2",
                owner_id=str(USER_OID),
            )
            with pytest.raises(GoneError):
                await svc.resolve(ALIAS)

    @pytest.mark.asyncio
    async def test_cache_miss_7char_tries_v2_first(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        url_cache.get.return_value = None
        doc = make_url_v2_doc(alias="abc1234")
        url_repo.find_by_alias.return_value = doc

        result, schema = await svc.resolve("abc1234")

        assert schema == "v2"
        assert result.alias == "abc1234"
        url_repo.find_by_alias.assert_called_once_with("abc1234", SYSTEM_DEFAULT_DOMAIN)
        legacy_repo.find_by_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_cache_miss_7char_falls_back_to_v1(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        url_cache.get.return_value = None
        url_repo.find_by_alias.return_value = None
        doc = make_legacy_doc(short_code="abc1234")
        legacy_repo.find_by_id.return_value = doc

        _result, schema = await svc.resolve("abc1234")

        assert schema == "v1"
        url_repo.find_by_alias.assert_called_once_with("abc1234", SYSTEM_DEFAULT_DOMAIN)
        legacy_repo.find_by_id.assert_called_once_with("abc1234")

    @pytest.mark.asyncio
    async def test_cache_miss_6char_tries_v1_first(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        url_cache.get.return_value = None
        doc = make_legacy_doc(short_code="abcdef")
        legacy_repo.find_by_id.return_value = doc

        _result, schema = await svc.resolve("abcdef")

        assert schema == "v1"
        legacy_repo.find_by_id.assert_called_once_with("abcdef")
        url_repo.find_by_alias.assert_not_called()

    @pytest.mark.asyncio
    async def test_cache_miss_6char_falls_back_to_v2(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        url_cache.get.return_value = None
        legacy_repo.find_by_id.return_value = None
        doc = make_url_v2_doc(alias="abcdef")
        url_repo.find_by_alias.return_value = doc

        _result, schema = await svc.resolve("abcdef")

        assert schema == "v2"
        legacy_repo.find_by_id.assert_called_once_with("abcdef")
        url_repo.find_by_alias.assert_called_once_with("abcdef", SYSTEM_DEFAULT_DOMAIN)

    @pytest.mark.asyncio
    async def test_cache_miss_emoji_resolves_emoji_schema(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        url_cache.get.return_value = None
        url_repo.find_by_alias.return_value = None  # v2 is tried first now
        emoji_doc = make_emoji_doc("🐍🔥💎")
        emoji_repo.find_by_id.return_value = emoji_doc

        _result, schema = await svc.resolve("🐍🔥💎")

        assert schema == "emoji"
        url_repo.find_by_alias.assert_called_once_with("🐍🔥💎", SYSTEM_DEFAULT_DOMAIN)
        emoji_repo.find_by_id.assert_called_once_with("🐍🔥💎")
        legacy_repo.find_by_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_cache_miss_emoji_v2_hit_wins_over_legacy(self):
        # v2-first ordering: an emoji alias stored in urlsV2 must answer
        # before the legacy emojis collection is even consulted.
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        url_cache.get.return_value = None
        url_repo.find_by_alias.return_value = make_url_v2_doc(alias="🐍🔥💎")

        _result, schema = await svc.resolve("🐍🔥💎")

        assert schema == "v2"
        emoji_repo.find_by_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_emoji_vs16_variant_resolves_canonical_v2_doc(self):
        # A pasted ⭐️-style variant (stray U+FE0F) must find the v2 doc
        # stored under the canonical alias, and cache under the canonical key.
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        vs16 = "️"
        url_cache.get.return_value = None
        url_repo.find_by_alias.return_value = make_url_v2_doc(alias="⭐🎉")

        _result, schema = await svc.resolve("⭐" + vs16 + "🎉")

        assert schema == "v2"
        url_cache.get.assert_called_once_with("⭐🎉", SYSTEM_DEFAULT_DOMAIN)
        url_repo.find_by_alias.assert_called_once_with("⭐🎉", SYSTEM_DEFAULT_DOMAIN)

    @pytest.mark.asyncio
    async def test_emoji_legacy_lookup_tries_raw_then_canonical(self):
        # Legacy emojis _ids keep their historical bytes: the raw request
        # form is tried first (exact legacy semantics), canonical second.
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        vs16 = "️"
        raw = "⭐" + vs16 + "🎉"
        url_cache.get.return_value = None
        url_repo.find_by_alias.return_value = None
        emoji_repo.find_by_id.side_effect = [None, make_emoji_doc("⭐🎉")]

        _result, schema = await svc.resolve(raw)

        assert schema == "emoji"
        assert [c.args[0] for c in emoji_repo.find_by_id.call_args_list] == [
            raw,
            "⭐🎉",
        ]

    @pytest.mark.asyncio
    async def test_cache_miss_other_length_tries_v2_first(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        url_cache.get.return_value = None
        doc = make_url_v2_doc(alias="customalias")
        url_repo.find_by_alias.return_value = doc

        _result, schema = await svc.resolve("customalias")

        assert schema == "v2"
        url_repo.find_by_alias.assert_called_once_with(
            "customalias", SYSTEM_DEFAULT_DOMAIN
        )

    @pytest.mark.asyncio
    async def test_cache_miss_not_found_raises_not_found(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        url_cache.get.return_value = None
        url_repo.find_by_alias.return_value = None
        legacy_repo.find_by_id.return_value = None

        with pytest.raises(NotFoundError):
            await svc.resolve("missing")

    @pytest.mark.asyncio
    async def test_db_miss_v2_blocked_caches_minimal_then_raises(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        url_cache.get.return_value = None
        blocked_doc = make_url_v2_doc(alias=ALIAS, status="BLOCKED")
        url_repo.find_by_alias.return_value = blocked_doc

        with pytest.raises(BlockedUrlError):
            await svc.resolve(ALIAS)

        # Cache should have been populated (even for blocked URLs)
        url_cache.set.assert_called_once()

    @pytest.mark.asyncio
    async def test_v1_with_max_clicks_not_cached(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        url_cache.get.return_value = None
        legacy_repo.find_by_id.return_value = make_legacy_doc(
            short_code="abcdef", max_clicks=10
        )

        await svc.resolve("abcdef")

        url_cache.set.assert_not_called()

    @pytest.mark.asyncio
    async def test_v1_without_max_clicks_is_cached(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        url_cache.get.return_value = None
        legacy_repo.find_by_id.return_value = make_legacy_doc(short_code="abcdef")

        await svc.resolve("abcdef")

        url_cache.set.assert_called_once()

    @pytest.mark.asyncio
    async def test_custom_domain_scope_only_hits_v2_with_domain(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        url_cache.get.return_value = None
        doc = make_url_v2_doc(alias=ALIAS, domain="links.acme.com")
        url_repo.find_by_alias.return_value = doc

        result, schema = await svc.resolve(ALIAS, domain="links.acme.com")

        assert schema == "v2"
        assert result.alias == ALIAS
        url_repo.find_by_alias.assert_called_once_with(ALIAS, "links.acme.com")
        legacy_repo.find_by_id.assert_not_called()
        emoji_repo.find_by_id.assert_not_called()
        url_cache.get.assert_called_once_with(ALIAS, "links.acme.com")

    @pytest.mark.asyncio
    async def test_custom_domain_unknown_alias_raises_not_found(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        url_cache.get.return_value = None
        url_repo.find_by_alias.return_value = None

        with pytest.raises(NotFoundError):
            await svc.resolve("absent", domain="links.acme.com")


# ─────────────────────────────────────────────────────────────────────────────
# TestUrlServiceCreate
# ─────────────────────────────────────────────────────────────────────────────


class TestUrlServiceCreate:
    @pytest.mark.asyncio
    async def test_creates_url_with_generated_alias(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        blocked_url_repo.get_patterns.return_value = []
        url_repo.check_alias_exists.return_value = False
        url_repo.insert.return_value = URL_OID

        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(long_url="https://example.com")
        result, _claim_token = await svc.create(
            req, owner_id=USER_OID, client_ip="1.2.3.4"
        )

        assert result.long_url == "https://example.com"
        url_repo.insert.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_stamps_destination_parts(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        blocked_url_repo.get_patterns.return_value = []
        url_repo.check_alias_exists.return_value = False
        url_repo.insert.return_value = URL_OID

        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(long_url="https://a.evil.co.uk/kit?x=1")
        await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")

        inserted = url_repo.insert.call_args.args[0]
        assert inserted["dest"]["registrable_domain"] == "evil.co.uk"
        assert inserted["dest"]["host"] == "a.evil.co.uk"
        assert inserted["dest"]["subdomain"] == "a"
        assert inserted["dest"]["scheme"] == "https"

    @pytest.mark.asyncio
    async def test_create_persists_created_via(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        blocked_url_repo.get_patterns.return_value = []
        url_repo.check_alias_exists.return_value = False
        url_repo.insert.return_value = URL_OID

        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(long_url="https://example.com")
        result, _claim_token = await svc.create(
            req, owner_id=USER_OID, client_ip="1.2.3.4", created_via="snap"
        )

        assert result.created_via == "snap"
        inserted = url_repo.insert.call_args.args[0]
        assert inserted["created_via"] == "snap"

    @pytest.mark.asyncio
    async def test_create_defaults_created_via_to_none(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        blocked_url_repo.get_patterns.return_value = []
        url_repo.check_alias_exists.return_value = False
        url_repo.insert.return_value = URL_OID

        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(long_url="https://example.com")
        result, _claim_token = await svc.create(
            req, owner_id=USER_OID, client_ip="1.2.3.4"
        )

        assert result.created_via is None

    @pytest.mark.asyncio
    async def test_create_with_custom_alias_checks_v2_uniqueness(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        blocked_url_repo.get_patterns.return_value = []
        # Alias does NOT exist in v2 or v1
        url_repo.check_alias_exists.return_value = False
        legacy_repo.check_exists.return_value = False
        url_repo.insert.return_value = URL_OID

        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(long_url="https://example.com", alias="myalias")
        await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")

        url_repo.check_alias_exists.assert_called_with("myalias", SYSTEM_DEFAULT_DOMAIN)

    @pytest.mark.asyncio
    async def test_create_with_custom_alias_checks_v1_uniqueness(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        blocked_url_repo.get_patterns.return_value = []
        # Not in v2
        url_repo.check_alias_exists.return_value = False
        # Exists in v1 → should reject
        legacy_repo.check_exists.return_value = True

        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(long_url="https://example.com", alias="myalias")
        with pytest.raises(ConflictError):
            await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")

    @pytest.mark.asyncio
    async def test_create_reserved_alias_raises_validation_error(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        blocked_url_repo.get_patterns.return_value = []
        url_repo.check_alias_exists.return_value = False
        legacy_repo.check_exists.return_value = False

        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(long_url="https://example.com", alias="pricing")
        with pytest.raises(ValidationError) as exc:
            await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")
        # Pin the error contract — the frontend keys inline feedback off it.
        assert exc.value.field == "alias"
        assert "reserved" in exc.value.message.lower()

    @pytest.mark.asyncio
    async def test_create_reserved_alias_allowed_on_custom_domain(self):
        # Custom-domain namespaces carry no frontend routes — "pricing"
        # only shadows a path on the default domain.
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        blocked_url_repo.get_patterns.return_value = []
        url_repo.check_alias_exists.return_value = False
        url_repo.insert.return_value = URL_OID

        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(long_url="https://example.com", alias="pricing")
        await svc.create(
            req, owner_id=USER_OID, client_ip="1.2.3.4", domain="go.example.com"
        )
        url_repo.insert.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_alias_reports_reserved(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        url_repo.check_alias_exists.return_value = False
        legacy_repo.check_exists.return_value = False

        assert await svc.check_alias("pricing") == "reserved"
        assert await svc.check_alias("pricing", domain="go.example.com") == "available"

    @pytest.mark.asyncio
    async def test_check_alias_earlier_checks_win_over_reserved(self, monkeypatch):
        # check_alias documents "first failing check wins". No real reserved
        # entry can fail length/format (test_reserved_aliases pins that
        # invariant), so force the reserved arm on to prove the ordering.
        import app.services.url_service as url_service_module

        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        monkeypatch.setattr(
            url_service_module, "is_reserved_alias", lambda _alias: True
        )

        assert await svc.check_alias("ab") == "length"
        assert await svc.check_alias("bad alias!") == "format"

    @pytest.mark.asyncio
    async def test_update_domain_move_rejects_reserved_alias_on_default(self):
        # A domain-only edit must not smuggle a reserved alias onto the
        # default domain — "pricing" is legal on go.example.com by design.
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        existing = make_url_v2_doc(alias="pricing", domain="go.example.com")
        url_repo.find_by_id.return_value = existing
        url_repo.check_alias_exists.return_value = False
        legacy_repo.check_exists.return_value = False

        from app.schemas.dto.requests.url import UpdateUrlRequest

        req = UpdateUrlRequest(domain=SYSTEM_DEFAULT_DOMAIN)
        with pytest.raises(ValidationError) as exc:
            await svc.update(URL_OID, req, USER_OID)
        assert exc.value.field == "domain"
        assert "reserved" in exc.value.message.lower()
        url_repo.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_blocked_url_raises_validation_error(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        blocked_url_repo.get_patterns.return_value = [r"https://evil\.com"]

        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(long_url="https://evil.com/page")
        with pytest.raises(ValidationError):
            await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")

    @pytest.mark.asyncio
    async def test_create_self_link_raises_validation_error(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        blocked_url_repo.get_patterns.return_value = []

        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(long_url="https://spoo.me/abc")
        with pytest.raises(ValidationError):
            await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")

    @pytest.mark.asyncio
    async def test_create_hashes_password(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        blocked_url_repo.get_patterns.return_value = []
        url_repo.check_alias_exists.return_value = False
        url_repo.insert.return_value = URL_OID

        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(long_url="https://example.com", password="Secret1!")
        await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")

        # password in DB doc should be a hash, not plaintext
        inserted_doc = url_repo.insert.call_args[0][0]
        assert inserted_doc["password"] != "Secret1!"
        assert inserted_doc["password"] is not None

    @pytest.mark.asyncio
    async def test_create_anonymous_owner_uses_sentinel(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        blocked_url_repo.get_patterns.return_value = []
        url_repo.check_alias_exists.return_value = False  # needed for alias generation
        url_repo.insert.return_value = URL_OID

        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(long_url="https://example.com")
        await svc.create(req, owner_id=None, client_ip="1.2.3.4")

        inserted_doc = url_repo.insert.call_args[0][0]
        assert inserted_doc["owner_id"] == ANONYMOUS_OWNER_ID

    @pytest.mark.asyncio
    async def test_create_stamps_system_default_domain(self):
        # Regression: empty domain on insert silently shadows real shorts under
        # the compound unique index. Service must always stamp it.
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        blocked_url_repo.get_patterns.return_value = []
        url_repo.check_alias_exists.return_value = False
        url_repo.insert.return_value = URL_OID

        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(long_url="https://example.com")
        await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")

        inserted_doc = url_repo.insert.call_args[0][0]
        assert inserted_doc["domain"] == SYSTEM_DEFAULT_DOMAIN

    @pytest.mark.asyncio
    async def test_create_future_expire_after_is_accepted(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        blocked_url_repo.get_patterns.return_value = []
        url_repo.check_alias_exists.return_value = False
        url_repo.insert.return_value = URL_OID

        from app.schemas.dto.requests.url import CreateUrlRequest

        # far future unix timestamp
        future_ts = 9999999999
        req = CreateUrlRequest(long_url="https://example.com", expire_after=future_ts)
        await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")

        inserted_doc = url_repo.insert.call_args[0][0]
        assert inserted_doc["expire_after"] is not None

    @pytest.mark.asyncio
    async def test_create_past_expire_after_raises_validation_error(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        blocked_url_repo.get_patterns.return_value = []

        from app.schemas.dto.requests.url import CreateUrlRequest

        past_ts = 1000000  # very old timestamp
        req = CreateUrlRequest(long_url="https://example.com", expire_after=past_ts)
        with pytest.raises(ValidationError):
            await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")


# ─────────────────────────────────────────────────────────────────────────────
# TestUrlServiceUpdate
# ─────────────────────────────────────────────────────────────────────────────


class TestUrlServiceCreateEmojiAlias:
    """Emoji aliases through the v2 create path — policy + canonical storage."""

    def _svc(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        blocked_url_repo.get_patterns.return_value = []
        url_repo.check_alias_exists.return_value = False
        legacy_repo.check_exists.return_value = False
        emoji_repo.check_exists_vs16_insensitive.return_value = False
        url_repo.insert.return_value = URL_OID
        return svc, url_repo, legacy_repo, emoji_repo

    @pytest.mark.asyncio
    async def test_custom_emoji_alias_stored_canonical(self):
        svc, url_repo, _legacy_repo, _emoji_repo = self._svc()
        from app.schemas.dto.requests.url import CreateUrlRequest

        vs16 = "️"
        req = CreateUrlRequest(long_url="https://example.com", alias="⭐" + vs16 + "🎉")
        result, _claim_token = await svc.create(
            req, owner_id=USER_OID, client_ip="1.2.3.4"
        )

        assert result.alias == "⭐🎉"
        inserted = url_repo.insert.call_args[0][0]
        assert inserted["alias"] == "⭐🎉"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("alias", ["🇺🇸", "🏳️‍🌈", "1️⃣", "🏿"])
    async def test_policy_rejected_emoji_raises_validation_error(self, alias):
        svc, *_ = self._svc()
        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(long_url="https://example.com", alias=alias)
        with pytest.raises(ValidationError) as exc:
            await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")
        assert exc.value.field == "alias"

    @pytest.mark.asyncio
    async def test_emoji_alias_too_many_graphemes_raises_length_error(self):
        svc, *_ = self._svc()
        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(long_url="https://example.com", alias="🎉" * 16)
        with pytest.raises(ValidationError) as exc:
            await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")
        assert "1-15" in exc.value.message

    @pytest.mark.asyncio
    async def test_emoji_alias_conflicts_with_legacy_vs16_variant(self):
        # A legacy emojis doc stored as ⭐️🎉 must block canonical ⭐🎉 —
        # otherwise the v2-first resolve order would shadow the live link.
        svc, _url_repo, _legacy_repo, emoji_repo = self._svc()
        emoji_repo.check_exists_vs16_insensitive.return_value = True
        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(long_url="https://example.com", alias="⭐🎉")
        with pytest.raises(ConflictError):
            await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")
        emoji_repo.check_exists_vs16_insensitive.assert_called_once_with("⭐🎉")

    @pytest.mark.asyncio
    async def test_alias_type_emoji_generates_policy_valid_alias(self):
        svc, url_repo, _legacy_repo, _emoji_repo = self._svc()
        from app.schemas.dto.requests.url import CreateUrlRequest
        from app.shared.emoji_policy import check_emoji_alias

        req = CreateUrlRequest(long_url="https://example.com", alias_type="emoji")
        result, _claim_token = await svc.create(
            req, owner_id=USER_OID, client_ip="1.2.3.4"
        )

        assert check_emoji_alias(result.alias) == "ok"
        inserted = url_repo.insert.call_args[0][0]
        assert inserted["alias"] == result.alias

    @pytest.mark.asyncio
    async def test_alias_type_emoji_generation_exhaustion_raises(self):
        svc, url_repo, *_ = self._svc()
        url_repo.check_alias_exists.return_value = True  # every candidate taken
        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(long_url="https://example.com", alias_type="emoji")
        with pytest.raises(AppError):
            await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")

    @pytest.mark.asyncio
    async def test_alias_type_ignored_when_alias_provided(self):
        svc, _url_repo, *_ = self._svc()
        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(
            long_url="https://example.com", alias="mylink", alias_type="emoji"
        )
        result, _claim_token = await svc.create(
            req, owner_id=USER_OID, client_ip="1.2.3.4"
        )
        assert result.alias == "mylink"


class TestCheckAliasEmoji:
    def _svc(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        url_repo.check_alias_exists.return_value = False
        legacy_repo.check_exists.return_value = False
        emoji_repo.check_exists_vs16_insensitive.return_value = False
        return svc, url_repo, emoji_repo

    @pytest.mark.asyncio
    async def test_valid_emoji_available(self):
        svc, *_ = self._svc()
        assert await svc.check_alias("🚀🔥") == "available"

    @pytest.mark.asyncio
    async def test_mixed_returns_format(self):
        svc, *_ = self._svc()
        assert await svc.check_alias("abc🎉") == "format"

    @pytest.mark.asyncio
    async def test_too_long_returns_length(self):
        svc, *_ = self._svc()
        assert await svc.check_alias("🎉" * 16) == "length"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("alias", ["🇺🇸", "🏳️‍🌈", "1️⃣"])
    async def test_rejected_sequences_return_emoji_policy(self, alias):
        svc, *_ = self._svc()
        assert await svc.check_alias(alias) == "emoji_policy"

    @pytest.mark.asyncio
    async def test_taken_in_v2_checked_canonically(self):
        svc, url_repo, _emoji_repo = self._svc()
        url_repo.check_alias_exists.return_value = True

        vs16 = "️"
        assert await svc.check_alias("⭐" + vs16 + "🎉") == "taken"
        url_repo.check_alias_exists.assert_called_with("⭐🎉", SYSTEM_DEFAULT_DOMAIN)

    @pytest.mark.asyncio
    async def test_taken_in_legacy_emoji_collection(self):
        svc, _url_repo, emoji_repo = self._svc()
        emoji_repo.check_exists_vs16_insensitive.return_value = True
        assert await svc.check_alias("🚀🔥") == "taken"


class TestUrlServiceUpdate:
    @pytest.mark.asyncio
    async def test_update_changes_field_and_invalidates_cache(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        existing = make_url_v2_doc()
        url_repo.find_by_id.return_value = existing
        url_repo.update.return_value = True

        from app.schemas.dto.requests.url import UpdateUrlRequest

        req = UpdateUrlRequest(long_url="https://new-url.com")
        await svc.update(URL_OID, req, USER_OID)

        url_repo.update.assert_called_once()
        update_doc = url_repo.update.call_args[0][1]
        assert "$set" in update_doc
        assert "long_url" in update_doc["$set"]
        url_cache.invalidate.assert_called_once_with(ALIAS, SYSTEM_DEFAULT_DOMAIN)

    @pytest.mark.asyncio
    async def test_update_long_url_restamps_dest(self):
        """A destination edit must re-stamp the parsed dest parts, or the
        indexed decomposition silently drifts from long_url."""
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        existing = make_url_v2_doc()
        url_repo.find_by_id.return_value = existing
        url_repo.update.return_value = True

        from app.schemas.dto.requests.url import UpdateUrlRequest

        req = UpdateUrlRequest(long_url="https://b.new-dest.com/x")
        await svc.update(URL_OID, req, USER_OID)

        written = url_repo.update.call_args[0][1]["$set"]
        assert written["dest"]["registrable_domain"] == "new-dest.com"
        assert written["dest"]["host"] == "b.new-dest.com"

    @pytest.mark.asyncio
    async def test_update_unparseable_long_url_unsets_dest(self, monkeypatch):
        """An unparseable new long_url removes dest, never writes null."""
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        existing = make_url_v2_doc()
        url_repo.find_by_id.return_value = existing
        url_repo.update.return_value = True

        from app.schemas.dto.requests.url import UpdateUrlRequest

        monkeypatch.setattr(
            "app.services.url_service.UrlDestination.for_link",
            classmethod(lambda cls, long_url, **_: None),
        )
        req = UpdateUrlRequest(long_url="https://b.new-dest.com/x")
        await svc.update(URL_OID, req, USER_OID)

        written = url_repo.update.call_args[0][1]
        assert "dest" not in written["$set"]
        assert written["$unset"] == {"dest": ""}

    @pytest.mark.asyncio
    async def test_update_meta_tags_stamps_client_ip(self):
        """A meta_tags write records the writer's IP for abuse forensics."""
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        blocked_url_repo.get_patterns.return_value = []
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        existing = make_url_v2_doc(owner_id=USER_OID)
        url_repo.find_by_id.return_value = existing

        from app.schemas.dto.requests.url import MetaTagsRequest, UpdateUrlRequest

        req = UpdateUrlRequest(meta_tags=MetaTagsRequest(title="New Card"))
        await svc.update(URL_OID, req, USER_OID, client_ip="9.9.9.9")

        written = url_repo.update.call_args[0][1]["$set"]["meta_tags"]
        assert written["title"] == "New Card"
        assert written["updated_ip"] == "9.9.9.9"

    @pytest.mark.asyncio
    async def test_update_no_changes_returns_existing(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        existing = make_url_v2_doc()
        url_repo.find_by_id.return_value = existing

        from app.schemas.dto.requests.url import UpdateUrlRequest

        # Send same long_url — no actual change
        req = UpdateUrlRequest(long_url="https://example.com")
        await svc.update(URL_OID, req, USER_OID)

        url_repo.update.assert_not_called()
        url_cache.invalidate.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_alias_to_emoji_stores_canonical(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        existing = make_url_v2_doc()
        url_repo.find_by_id.return_value = existing
        url_repo.update.return_value = True
        url_repo.check_alias_exists.return_value = False
        legacy_repo.check_exists.return_value = False
        emoji_repo.check_exists_vs16_insensitive.return_value = False

        from app.schemas.dto.requests.url import UpdateUrlRequest

        vs16 = "️"
        req = UpdateUrlRequest(alias="⭐" + vs16 + "🎉")
        await svc.update(URL_OID, req, USER_OID)

        update_doc = url_repo.update.call_args[0][1]
        assert update_doc["$set"]["alias"] == "⭐🎉"

    @pytest.mark.asyncio
    async def test_update_alias_vs16_echo_of_current_is_noop(self):
        # PATCHing back a VS16 variant of the current alias must not
        # self-collide or write — canonical comparison makes it a no-op.
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        existing = make_url_v2_doc(alias="⭐🎉")
        url_repo.find_by_id.return_value = existing

        from app.schemas.dto.requests.url import UpdateUrlRequest

        vs16 = "️"
        req = UpdateUrlRequest(alias="⭐" + vs16 + "🎉")
        await svc.update(URL_OID, req, USER_OID)

        url_repo.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_alias_policy_rejected_emoji_raises(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        existing = make_url_v2_doc()
        url_repo.find_by_id.return_value = existing

        from app.schemas.dto.requests.url import UpdateUrlRequest

        req = UpdateUrlRequest(alias="🇺🇸")
        with pytest.raises(ValidationError) as exc:
            await svc.update(URL_OID, req, USER_OID)
        assert exc.value.field == "alias"

    @pytest.mark.asyncio
    async def test_update_wrong_owner_raises_forbidden(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        existing = make_url_v2_doc(owner_id=USER_OID)
        url_repo.find_by_id.return_value = existing

        other_user = ObjectId("eeeeeeeeeeeeeeeeeeeeeeee")

        from app.schemas.dto.requests.url import UpdateUrlRequest

        req = UpdateUrlRequest(long_url="https://new-url.com")
        with pytest.raises(ForbiddenError):
            await svc.update(URL_OID, req, other_user)

    @pytest.mark.asyncio
    async def test_update_not_found_raises_not_found(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        url_repo.find_by_id.return_value = None

        from app.schemas.dto.requests.url import UpdateUrlRequest

        req = UpdateUrlRequest(long_url="https://new-url.com")
        with pytest.raises(NotFoundError):
            await svc.update(URL_OID, req, USER_OID)

    @pytest.mark.asyncio
    async def test_update_alias_conflict_raises_conflict(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        existing = make_url_v2_doc()
        url_repo.find_by_id.return_value = existing
        # New alias already exists in v2
        url_repo.check_alias_exists.return_value = True

        from app.schemas.dto.requests.url import UpdateUrlRequest

        req = UpdateUrlRequest(alias="taken")
        with pytest.raises(ConflictError):
            await svc.update(URL_OID, req, USER_OID)

    @pytest.mark.asyncio
    async def test_update_blocked_url_raises_forbidden(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        existing = make_url_v2_doc(status="BLOCKED")
        url_repo.find_by_id.return_value = existing

        from app.schemas.dto.requests.url import UpdateUrlRequest

        req = UpdateUrlRequest(long_url="https://new-url.com")
        with pytest.raises(ForbiddenError, match="Cannot modify a blocked URL"):
            await svc.update(URL_OID, req, USER_OID)

        url_repo.update.assert_not_called()
        url_cache.invalidate.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_blocked_url_status_change_raises_forbidden(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        existing = make_url_v2_doc(status="BLOCKED")
        url_repo.find_by_id.return_value = existing

        from app.schemas.dto.requests.url import UpdateUrlRequest

        req = UpdateUrlRequest(status="ACTIVE")
        with pytest.raises(ForbiddenError, match="Cannot modify a blocked URL"):
            await svc.update(URL_OID, req, USER_OID)

        url_repo.update.assert_not_called()
        url_cache.invalidate.assert_not_called()

    # ── Domain move (Part C) ─────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_update_domain_move_invalidates_both_cache_keys(self):
        """Moving a URL between domains must clear the old AND new cache keys
        so a worker that populated the new key during the rename can't serve
        stale data."""
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        existing = make_url_v2_doc(domain="spoo.me")
        url_repo.find_by_id.return_value = existing
        url_repo.update.return_value = True
        # Alias is free on the target tenant.
        url_repo.check_alias_exists.return_value = False
        legacy_repo.check_exists.return_value = False

        from app.schemas.dto.requests.url import UpdateUrlRequest

        req = UpdateUrlRequest(domain="links.acme.com")
        await svc.update(URL_OID, req, USER_OID)

        update_doc = url_repo.update.call_args[0][1]
        assert update_doc["$set"]["domain"] == "links.acme.com"
        # Both keys cleared — order doesn't matter, just both calls happened.
        invalidated = {tuple(c.args) for c in url_cache.invalidate.await_args_list}
        assert (ALIAS, "spoo.me") in invalidated
        assert (ALIAS, "links.acme.com") in invalidated

    @pytest.mark.asyncio
    async def test_update_domain_unchanged_is_noop(self):
        """`domain` set to the URL's current value shouldn't trigger a write."""
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        existing = make_url_v2_doc(domain="links.acme.com")
        url_repo.find_by_id.return_value = existing

        from app.schemas.dto.requests.url import UpdateUrlRequest

        req = UpdateUrlRequest(domain="links.acme.com")
        await svc.update(URL_OID, req, USER_OID)

        url_repo.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_domain_null_moves_to_system_default(self):
        """Passing `domain=null` moves the URL back to the system default."""
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        existing = make_url_v2_doc(domain="links.acme.com")
        url_repo.find_by_id.return_value = existing
        url_repo.update.return_value = True
        url_repo.check_alias_exists.return_value = False
        legacy_repo.check_exists.return_value = False

        from app.schemas.dto.requests.url import UpdateUrlRequest

        # `UpdateUrlRequest(domain=None)` populates model_fields_set via the
        # constructor signature; using model_validate keeps it explicit that
        # we're sending a "clear to default" intent vs. an omitted field.
        req = UpdateUrlRequest.model_validate({"domain": None})
        await svc.update(URL_OID, req, USER_OID)

        update_doc = url_repo.update.call_args[0][1]
        assert update_doc["$set"]["domain"] == SYSTEM_DEFAULT_DOMAIN

    @pytest.mark.asyncio
    async def test_update_domain_alias_collision_on_target_raises_conflict(self):
        """An alias that's taken on the target domain blocks the move with a
        409, even if it's free on the source."""
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        existing = make_url_v2_doc(domain="spoo.me")
        url_repo.find_by_id.return_value = existing
        # Target domain already has this alias.
        url_repo.check_alias_exists.return_value = True

        from app.schemas.dto.requests.url import UpdateUrlRequest

        req = UpdateUrlRequest(domain="links.acme.com")
        with pytest.raises(ConflictError, match="is already in use on"):
            await svc.update(URL_OID, req, USER_OID)

        url_repo.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_alias_and_domain_together_checks_target_combo(self):
        """When both fields change in one request, the alias collision check
        must scope to the *target* domain — not the source."""
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        existing = make_url_v2_doc(alias=ALIAS, domain="spoo.me")
        url_repo.find_by_id.return_value = existing
        url_repo.update.return_value = True
        url_repo.check_alias_exists.return_value = False
        legacy_repo.check_exists.return_value = False

        from app.schemas.dto.requests.url import UpdateUrlRequest

        req = UpdateUrlRequest(alias="newalias", domain="links.acme.com")
        await svc.update(URL_OID, req, USER_OID)

        # Pin the scope of the collision check — it should be the target
        # tenant, not the current one. Every call's domain arg must be the
        # target. (Domain handler short-circuits when ops["alias"] was set,
        # so only the alias handler should fire its check.)
        assert url_repo.check_alias_exists.await_count >= 1
        for call in url_repo.check_alias_exists.await_args_list:
            assert call.args[1] == "links.acme.com"

        update_doc = url_repo.update.call_args[0][1]["$set"]
        assert update_doc["alias"] == "newalias"
        assert update_doc["domain"] == "links.acme.com"


# ─────────────────────────────────────────────────────────────────────────────
# TestUrlServiceAutoReactivate
# ─────────────────────────────────────────────────────────────────────────────


class TestUrlServiceAutoReactivate:
    """Expired URLs should auto-reactivate when expiry conditions are updated."""

    @pytest.mark.asyncio
    async def test_raising_max_clicks_reactivates_expired_url(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        existing = UrlV2Doc.from_mongo(
            {
                "_id": URL_OID,
                "alias": ALIAS,
                "owner_id": USER_OID,
                "domain": SYSTEM_DEFAULT_DOMAIN,
                "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
                "long_url": "https://example.com",
                "status": "EXPIRED",
                "max_clicks": 3,
                "total_clicks": 3,
                "private_stats": True,
            }
        )
        url_repo.find_by_id.return_value = existing
        url_repo.update.return_value = True

        from app.schemas.dto.requests.url import UpdateUrlRequest

        req = UpdateUrlRequest(max_clicks=10)
        await svc.update(URL_OID, req, USER_OID)

        update_doc = url_repo.update.call_args[0][1]
        assert update_doc["$set"]["status"] == "ACTIVE"
        assert update_doc["$set"]["max_clicks"] == 10

    @pytest.mark.asyncio
    async def test_clearing_max_clicks_reactivates_expired_url(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        existing = UrlV2Doc.from_mongo(
            {
                "_id": URL_OID,
                "alias": ALIAS,
                "owner_id": USER_OID,
                "domain": SYSTEM_DEFAULT_DOMAIN,
                "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
                "long_url": "https://example.com",
                "status": "EXPIRED",
                "max_clicks": 3,
                "total_clicks": 3,
                "private_stats": True,
            }
        )
        url_repo.find_by_id.return_value = existing
        url_repo.update.return_value = True

        from app.schemas.dto.requests.url import UpdateUrlRequest

        req = UpdateUrlRequest(max_clicks=0)
        await svc.update(URL_OID, req, USER_OID)

        update_doc = url_repo.update.call_args[0][1]
        assert update_doc["$set"]["status"] == "ACTIVE"
        assert update_doc["$set"]["max_clicks"] is None

    @pytest.mark.asyncio
    async def test_extending_expire_after_reactivates_expired_url(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        existing = UrlV2Doc.from_mongo(
            {
                "_id": URL_OID,
                "alias": ALIAS,
                "owner_id": USER_OID,
                "domain": SYSTEM_DEFAULT_DOMAIN,
                "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
                "long_url": "https://example.com",
                "status": "EXPIRED",
                "expire_after": datetime(2024, 6, 1, tzinfo=timezone.utc),
                "private_stats": True,
            }
        )
        url_repo.find_by_id.return_value = existing
        url_repo.update.return_value = True

        from app.schemas.dto.requests.url import UpdateUrlRequest

        req = UpdateUrlRequest(expire_after="2030-01-01T00:00:00Z")
        await svc.update(URL_OID, req, USER_OID)

        update_doc = url_repo.update.call_args[0][1]
        assert update_doc["$set"]["status"] == "ACTIVE"

    @pytest.mark.asyncio
    async def test_explicit_status_overrides_auto_reactivate(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        existing = UrlV2Doc.from_mongo(
            {
                "_id": URL_OID,
                "alias": ALIAS,
                "owner_id": USER_OID,
                "domain": SYSTEM_DEFAULT_DOMAIN,
                "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
                "long_url": "https://example.com",
                "status": "EXPIRED",
                "max_clicks": 3,
                "total_clicks": 3,
                "private_stats": True,
            }
        )
        url_repo.find_by_id.return_value = existing
        url_repo.update.return_value = True

        from app.schemas.dto.requests.url import UpdateUrlRequest

        req = UpdateUrlRequest(max_clicks=10, status="INACTIVE")
        await svc.update(URL_OID, req, USER_OID)

        update_doc = url_repo.update.call_args[0][1]
        assert update_doc["$set"]["status"] == "INACTIVE"

    @pytest.mark.asyncio
    async def test_no_reactivate_when_max_clicks_still_exceeded(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        existing = UrlV2Doc.from_mongo(
            {
                "_id": URL_OID,
                "alias": ALIAS,
                "owner_id": USER_OID,
                "domain": SYSTEM_DEFAULT_DOMAIN,
                "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
                "long_url": "https://example.com",
                "status": "EXPIRED",
                "max_clicks": 3,
                "total_clicks": 5,
                "private_stats": True,
            }
        )
        url_repo.find_by_id.return_value = existing
        url_repo.update.return_value = True

        from app.schemas.dto.requests.url import UpdateUrlRequest

        # Raising to 4 still below total_clicks of 5
        req = UpdateUrlRequest(max_clicks=4)
        await svc.update(URL_OID, req, USER_OID)

        update_doc = url_repo.update.call_args[0][1]
        assert "status" not in update_doc["$set"]


# ─────────────────────────────────────────────────────────────────────────────
# TestUrlServiceDelete
# ─────────────────────────────────────────────────────────────────────────────


class TestUrlServiceDelete:
    @pytest.mark.asyncio
    async def test_delete_success_invalidates_cache(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        existing = make_url_v2_doc()
        url_repo.find_by_id.return_value = existing
        url_repo.delete.return_value = True

        await svc.delete(URL_OID, USER_OID)

        url_repo.delete.assert_called_once_with(URL_OID)
        url_cache.invalidate.assert_called_once_with(ALIAS, SYSTEM_DEFAULT_DOMAIN)

    @pytest.mark.asyncio
    async def test_delete_wrong_owner_raises_forbidden(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        existing = make_url_v2_doc(owner_id=USER_OID)
        url_repo.find_by_id.return_value = existing

        other_user = ObjectId("eeeeeeeeeeeeeeeeeeeeeeee")
        with pytest.raises(ForbiddenError):
            await svc.delete(URL_OID, other_user)

        url_repo.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_not_found_raises_not_found(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        url_repo.find_by_id.return_value = None

        with pytest.raises(NotFoundError):
            await svc.delete(URL_OID, USER_OID)

    @pytest.mark.asyncio
    async def test_delete_blocked_url_raises_forbidden(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        existing = make_url_v2_doc(status="BLOCKED")
        url_repo.find_by_id.return_value = existing

        with pytest.raises(ForbiddenError, match="Cannot delete a blocked URL"):
            await svc.delete(URL_OID, USER_OID)

        url_repo.delete.assert_not_called()
        url_cache.invalidate.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# TestCheckAliasAvailable
# ─────────────────────────────────────────────────────────────────────────────


class TestCheckAliasAvailable:
    @pytest.mark.asyncio
    async def test_available_when_not_in_v2_or_v1(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        url_repo.check_alias_exists.return_value = False
        legacy_repo.check_exists.return_value = False

        assert await svc.check_alias_available("newcode") is True

    @pytest.mark.asyncio
    async def test_unavailable_when_in_v2(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        url_repo.check_alias_exists.return_value = True

        assert await svc.check_alias_available("taken") is False
        legacy_repo.check_exists.assert_not_called()

    @pytest.mark.asyncio
    async def test_unavailable_when_in_v1(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        url_repo.check_alias_exists.return_value = False
        legacy_repo.check_exists.return_value = True

        assert await svc.check_alias_available("v1code") is False


# ─────────────────────────────────────────────────────────────────────────────
# TestUrlServiceUpdate
# ─────────────────────────────────────────────────────────────────────────────


class TestUrlServiceUpdateEdgeCases:
    @pytest.mark.asyncio
    async def test_update_not_found_raises(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        url_repo.find_by_id.return_value = None
        from app.schemas.dto.requests.url import UpdateUrlRequest

        with pytest.raises(NotFoundError):
            await svc.update(URL_OID, UpdateUrlRequest(), USER_OID)

    @pytest.mark.asyncio
    async def test_update_forbidden_raises(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        other_user = ObjectId("bbbbbbbbbbbbbbbbbbbbbbbb")
        url_repo.find_by_id.return_value = make_url_v2_doc(owner_id=other_user)
        from app.schemas.dto.requests.url import UpdateUrlRequest

        with pytest.raises(ForbiddenError):
            await svc.update(URL_OID, UpdateUrlRequest(), USER_OID)

    @pytest.mark.asyncio
    async def test_update_no_op_returns_existing(self):
        """When nothing changes, update() returns the existing doc without hitting the DB."""
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        existing = make_url_v2_doc()
        url_repo.find_by_id.return_value = existing
        from app.schemas.dto.requests.url import UpdateUrlRequest

        # Empty request — nothing in model_fields_set, no long_url or alias given
        result = await svc.update(URL_OID, UpdateUrlRequest(), USER_OID)

        url_repo.update.assert_not_awaited()
        url_cache.invalidate.assert_not_awaited()
        assert result is existing

    @pytest.mark.asyncio
    async def test_update_clears_password_when_set_to_none(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        existing = make_url_v2_doc(password="oldhash")
        url_repo.find_by_id.return_value = existing
        url_repo.update.return_value = True
        url_cache.invalidate.return_value = None

        from app.schemas.dto.requests.url import UpdateUrlRequest

        req = UpdateUrlRequest(password=None)
        # Pydantic v2: explicitly passing password=None puts it in model_fields_set
        assert "password" in req.model_fields_set

        await svc.update(URL_OID, req, USER_OID)

        call_args = url_repo.update.call_args[0][1]
        assert call_args["$set"]["password"] is None

    @pytest.mark.asyncio
    async def test_update_clears_max_clicks_when_set_to_zero(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        existing = make_url_v2_doc(max_clicks=100)
        url_repo.find_by_id.return_value = existing
        url_repo.update.return_value = True

        from app.schemas.dto.requests.url import UpdateUrlRequest

        req = UpdateUrlRequest(max_clicks=0)
        await svc.update(URL_OID, req, USER_OID)

        call_args = url_repo.update.call_args[0][1]
        assert call_args["$set"]["max_clicks"] is None

    @pytest.mark.asyncio
    async def test_update_clears_expire_after_when_set_to_none(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        existing = make_url_v2_doc(
            expire_after=datetime(2030, 1, 1, tzinfo=timezone.utc)
        )
        url_repo.find_by_id.return_value = existing
        url_repo.update.return_value = True

        from app.schemas.dto.requests.url import UpdateUrlRequest

        req = UpdateUrlRequest(expire_after=None)
        await svc.update(URL_OID, req, USER_OID)

        call_args = url_repo.update.call_args[0][1]
        assert call_args["$set"]["expire_after"] is None

    @pytest.mark.asyncio
    async def test_update_alias_conflict_raises(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        existing = make_url_v2_doc(alias="old123")
        url_repo.find_by_id.return_value = existing
        # alias is taken
        url_repo.check_alias_exists.return_value = True
        legacy_repo.check_exists.return_value = False

        from app.schemas.dto.requests.url import UpdateUrlRequest

        req = UpdateUrlRequest(alias="newcode")
        with pytest.raises(ConflictError):
            await svc.update(URL_OID, req, USER_OID)

    @pytest.mark.asyncio
    async def test_update_changes_block_bots(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        existing = make_url_v2_doc(block_bots=False)
        url_repo.find_by_id.return_value = existing
        url_repo.update.return_value = True

        from app.schemas.dto.requests.url import UpdateUrlRequest

        req = UpdateUrlRequest(block_bots=True)
        await svc.update(URL_OID, req, USER_OID)

        call_args = url_repo.update.call_args[0][1]
        assert call_args["$set"]["block_bots"] is True


# ─────────────────────────────────────────────────────────────────────────────
# TestUrlServiceListByOwner
# ─────────────────────────────────────────────────────────────────────────────


class TestUrlServiceListByOwner:
    @pytest.mark.asyncio
    async def test_list_no_filter_returns_pagination(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        url_repo.count_by_query.return_value = 1
        url_repo.find_by_owner.return_value = [make_url_v2_doc()]

        from app.schemas.dto.requests.url import ListUrlsQuery

        result = await svc.list_by_owner(USER_OID, ListUrlsQuery())

        assert result["total"] == 1
        assert result["page"] == 1
        assert len(result["items"]) == 1
        assert result["hasNext"] is False

    @pytest.mark.asyncio
    async def test_list_with_status_filter(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        url_repo.count_by_query.return_value = 0
        url_repo.find_by_owner.return_value = []

        from app.schemas.dto.requests.url import ListUrlsQuery

        q = ListUrlsQuery(filter='{"status": "INACTIVE"}')
        await svc.list_by_owner(USER_OID, q)

        call_query = url_repo.count_by_query.call_args[0][0]
        assert call_query.get("status") == "INACTIVE"

    @pytest.mark.asyncio
    async def test_list_with_search_filter(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        url_repo.count_by_query.return_value = 0
        url_repo.find_by_owner.return_value = []

        from app.schemas.dto.requests.url import ListUrlsQuery

        q = ListUrlsQuery(filter='{"search": "example"}')
        await svc.list_by_owner(USER_OID, q)

        call_query = url_repo.count_by_query.call_args[0][0]
        assert "$or" in call_query

    @pytest.mark.asyncio
    async def test_list_with_password_set_filter(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        url_repo.count_by_query.return_value = 0
        url_repo.find_by_owner.return_value = []

        from app.schemas.dto.requests.url import ListUrlsQuery

        q = ListUrlsQuery(filter='{"passwordSet": true}')
        await svc.list_by_owner(USER_OID, q)

        call_query = url_repo.count_by_query.call_args[0][0]
        assert call_query.get("password") == {"$ne": None}

    @pytest.mark.asyncio
    async def test_list_with_max_clicks_set_false_filter(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        url_repo.count_by_query.return_value = 0
        url_repo.find_by_owner.return_value = []

        from app.schemas.dto.requests.url import ListUrlsQuery

        q = ListUrlsQuery(filter='{"maxClicksSet": false}')
        await svc.list_by_owner(USER_OID, q)

        call_query = url_repo.count_by_query.call_args[0][0]
        assert call_query.get("max_clicks") is None

    @pytest.mark.asyncio
    async def test_list_has_next_when_more_pages(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        url_repo.count_by_query.return_value = 50
        url_repo.find_by_owner.return_value = [make_url_v2_doc()] * 20

        from app.schemas.dto.requests.url import ListUrlsQuery

        result = await svc.list_by_owner(USER_OID, ListUrlsQuery(pageSize=20))

        assert result["hasNext"] is True
        assert result["total"] == 50


# ─────────────────────────────────────────────────────────────────────────────
# TestUrlServiceCreateOnCustomDomain
# ─────────────────────────────────────────────────────────────────────────────


class TestUrlServiceCreateOnCustomDomain:
    """create(..., domain=) writes the doc on the right tenant and routes
    alias-availability checks through the right namespace."""

    @pytest.mark.asyncio
    async def test_create_uses_provided_domain_for_doc_and_alias_check(self):
        from app.schemas.dto.requests.url import CreateUrlRequest

        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        url_repo.check_alias_exists = AsyncMock(return_value=False)
        url_repo.insert = AsyncMock(return_value=ObjectId())
        blocked_url_repo.get_patterns = AsyncMock(return_value=[])

        req = CreateUrlRequest(
            url="https://example.com/x",
            alias="myalias",
        )

        await svc.create(req, USER_OID, "1.2.3.4", domain="links.acme.com")

        # Alias-availability scoped to custom domain — and only that namespace
        # (no legacy fallback check).
        url_repo.check_alias_exists.assert_awaited_once_with(
            "myalias", "links.acme.com"
        )
        legacy_repo.check_exists.assert_not_called()

        # Doc inserted carries the custom domain.
        inserted = url_repo.insert.call_args[0][0]
        assert inserted["domain"] == "links.acme.com"

    @pytest.mark.asyncio
    async def test_create_auto_generates_alias_against_custom_domain(self):
        # Regression guard: previously _generate_unique_alias() probed the
        # system default namespace regardless of which tenant the URL was
        # being created on. With the domain= kwarg threaded through, the
        # candidate must be checked against the custom domain.
        from app.schemas.dto.requests.url import CreateUrlRequest

        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        url_repo.check_alias_exists = AsyncMock(return_value=False)
        url_repo.insert = AsyncMock(return_value=ObjectId())
        blocked_url_repo.get_patterns = AsyncMock(return_value=[])

        # No alias provided → service auto-generates one.
        req = CreateUrlRequest(url="https://example.com/auto-alias")

        await svc.create(req, USER_OID, "1.2.3.4", domain="links.acme.com")

        # The probe was scoped to the custom domain, never the system default.
        url_repo.check_alias_exists.assert_awaited_once()
        _, probed_domain = url_repo.check_alias_exists.call_args.args
        assert probed_domain == "links.acme.com"

        # Persisted doc lands on the custom tenant.
        inserted = url_repo.insert.call_args[0][0]
        assert inserted["domain"] == "links.acme.com"

    @pytest.mark.asyncio
    async def test_create_with_no_domain_uses_system_default(self):
        from app.schemas.dto.requests.url import CreateUrlRequest

        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        url_repo.check_alias_exists = AsyncMock(return_value=False)
        legacy_repo.check_exists = AsyncMock(return_value=False)
        url_repo.insert = AsyncMock(return_value=ObjectId())
        blocked_url_repo.get_patterns = AsyncMock(return_value=[])

        req = CreateUrlRequest(url="https://example.com/x", alias="mine")

        await svc.create(req, USER_OID, "1.2.3.4")

        # System default uses both v2 + legacy alias check.
        url_repo.check_alias_exists.assert_awaited_once_with(
            "mine", SYSTEM_DEFAULT_DOMAIN
        )
        legacy_repo.check_exists.assert_awaited_once_with("mine")
        inserted = url_repo.insert.call_args[0][0]
        assert inserted["domain"] == SYSTEM_DEFAULT_DOMAIN


# ─────────────────────────────────────────────────────────────────────────────
# TestUrlServiceBulkDelete
# ─────────────────────────────────────────────────────────────────────────────


class TestUrlServiceBulkDelete:
    @pytest.mark.asyncio
    async def test_bulk_delete_refuses_system_default(self):
        from app.errors import ValidationError

        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        with pytest.raises(ValidationError):
            await svc.delete_all_by_domain(USER_OID, SYSTEM_DEFAULT_DOMAIN)
        url_repo.delete_many_by_owner_and_domain.assert_not_called()

    @pytest.mark.asyncio
    async def test_bulk_delete_zero_match_returns_zero(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        url_repo.list_aliases_by_owner_and_domain = AsyncMock(return_value=[])
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        count = await svc.delete_all_by_domain(USER_OID, "links.acme.com")
        assert count == 0
        url_repo.delete_many_by_owner_and_domain.assert_not_called()
        url_cache.invalidate_many.assert_not_called()

    @pytest.mark.asyncio
    async def test_bulk_delete_invalidates_cache_after_delete(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        url_repo.list_aliases_by_owner_and_domain = AsyncMock(
            return_value=["a", "b", "c"]
        )
        url_repo.delete_many_by_owner_and_domain = AsyncMock(return_value=3)
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        count = await svc.delete_all_by_domain(USER_OID, "links.acme.com")

        assert count == 3
        # Interactive default: BLOCKED docs on the fqdn are deleted too,
        # and nothing gets scrubbed.
        url_repo.delete_many_by_owner_and_domain.assert_awaited_once_with(
            USER_OID, "links.acme.com", retain_blocked=False
        )
        url_repo.scrub_blocked_owner_pii.assert_not_called()
        url_cache.invalidate_many.assert_awaited_once_with(
            ["a", "b", "c"], "links.acme.com"
        )

    @pytest.mark.asyncio
    async def test_bulk_delete_retain_blocked_scrubs_then_excludes(self):
        """Erasure's domain cascade: BLOCKED docs on the fqdn survive with
        creator PII scrubbed; only the rest are deleted — so the
        blocked-retained tally from the owner-wide step stays exact."""
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        url_repo.list_aliases_by_owner_and_domain = AsyncMock(
            return_value=["a", "badbad1"]
        )
        url_repo.scrub_blocked_owner_pii = AsyncMock(return_value=1)
        url_repo.delete_many_by_owner_and_domain = AsyncMock(return_value=1)
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        count = await svc.delete_all_by_domain(
            USER_OID, "links.acme.com", retain_blocked=True
        )

        assert count == 1
        url_repo.scrub_blocked_owner_pii.assert_awaited_once_with(
            USER_OID, domain="links.acme.com"
        )
        url_repo.delete_many_by_owner_and_domain.assert_awaited_once_with(
            USER_OID, "links.acme.com", retain_blocked=True
        )
        # Retained BLOCKED aliases still get their cache entries purged —
        # the cached projection carries the pre-scrub password hash.
        url_cache.invalidate_many.assert_awaited_once_with(
            ["a", "badbad1"], "links.acme.com"
        )


# ─────────────────────────────────────────────────────────────────────────────
# TestUrlServiceListByOwnerDomainFilter
# ─────────────────────────────────────────────────────────────────────────────


class TestUrlServiceListByOwnerDomainFilter:
    @pytest.mark.asyncio
    async def test_list_with_domain_filter_passes_to_query(self):
        from app.schemas.dto.requests.url import ListUrlsQuery

        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        url_repo.count_by_query.return_value = 0
        url_repo.find_by_owner.return_value = []

        q = ListUrlsQuery(domain="links.acme.com")
        await svc.list_by_owner(USER_OID, q)

        call_query = url_repo.count_by_query.call_args[0][0]
        assert call_query.get("domain") == "links.acme.com"

    @pytest.mark.asyncio
    async def test_list_without_domain_filter_omits_field(self):
        from app.schemas.dto.requests.url import ListUrlsQuery

        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        url_repo.count_by_query.return_value = 0
        url_repo.find_by_owner.return_value = []

        q = ListUrlsQuery()
        await svc.list_by_owner(USER_OID, q)

        call_query = url_repo.count_by_query.call_args[0][0]
        assert "domain" not in call_query


# ─────────────────────────────────────────────────────────────────────────────
# TestUrlServiceGeoRules
# ─────────────────────────────────────────────────────────────────────────────


class TestGeoRulesFeatureGate:
    @pytest.mark.asyncio
    async def test_geo_rules_refused_while_the_feature_is_dark(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo,
            legacy_repo,
            emoji_repo,
            blocked_url_repo,
            url_cache,
            geo_rules_enabled=False,
        )
        blocked_url_repo.get_patterns.return_value = []

        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(
            long_url="https://example.com", geo_rules={"IN": "https://example.in/"}
        )
        with pytest.raises(ValidationError) as exc:
            await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")
        assert exc.value.field == "geo_rules"
        url_repo.insert.assert_not_awaited()


class TestUrlServiceExpiredFallback:
    """expired_redirect_url: validated like a destination, stamped as a
    secondary host, and served instead of 410 once the link is EXPIRED."""

    FALLBACK = "https://fallback.example/ended"

    def _svc(self, patterns=()):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        blocked_url_repo.get_patterns.return_value = list(patterns)
        url_repo.check_alias_exists.return_value = False
        url_repo.insert.return_value = URL_OID
        return svc, url_repo, url_cache

    @pytest.mark.asyncio
    async def test_create_persists_fallback_and_stamps_its_host(self):
        svc, url_repo, _ = self._svc()
        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(
            long_url="https://example.com", expired_redirect_url=self.FALLBACK
        )
        result, _ = await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")

        assert result.expired_redirect_url == self.FALLBACK
        inserted = url_repo.insert.call_args[0][0]
        assert inserted["expired_redirect_url"] == self.FALLBACK
        assert inserted["dest"]["secondary_hosts"] == ["fallback.example"]

    @pytest.mark.asyncio
    async def test_create_blocked_fallback_rejected_with_field_path(self):
        svc, url_repo, _ = self._svc(patterns=[r"https://evil\.com"])
        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(
            long_url="https://example.com",
            expired_redirect_url="https://evil.com/landing",
        )
        with pytest.raises(ValidationError) as exc:
            await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")
        assert exc.value.field == "expired_redirect_url"
        url_repo.insert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_self_link_fallback_rejected(self):
        svc, _, _ = self._svc()
        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(
            long_url="https://example.com",
            expired_redirect_url="https://spoo.me/other",
        )
        with pytest.raises(ValidationError) as exc:
            await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")
        assert exc.value.field == "expired_redirect_url"

    @pytest.mark.asyncio
    async def test_update_sets_fallback_and_restamps_dest(self):
        svc, url_repo, url_cache = self._svc()
        url_repo.find_by_id.return_value = make_url_v2_doc()
        url_repo.update.return_value = True

        req = UpdateUrlRequest(expired_redirect_url=self.FALLBACK)
        await svc.update(URL_OID, req, USER_OID)

        update_doc = url_repo.update.call_args[0][1]
        assert update_doc["$set"]["expired_redirect_url"] == self.FALLBACK
        assert update_doc["$set"]["dest"]["secondary_hosts"] == ["fallback.example"]
        url_cache.invalidate.assert_called_once_with(ALIAS, SYSTEM_DEFAULT_DOMAIN)

    @pytest.mark.asyncio
    async def test_update_blocked_fallback_rejected(self):
        svc, url_repo, _ = self._svc(patterns=[r"https://evil\.com"])
        url_repo.find_by_id.return_value = make_url_v2_doc()

        req = UpdateUrlRequest(expired_redirect_url="https://evil.com/x")
        with pytest.raises(ValidationError) as exc:
            await svc.update(URL_OID, req, USER_OID)
        assert exc.value.field == "expired_redirect_url"
        url_repo.update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_update_null_clears_fallback_and_its_host(self):
        svc, url_repo, _ = self._svc()
        url_repo.find_by_id.return_value = make_url_v2_doc(
            expired_redirect_url=self.FALLBACK
        )
        url_repo.update.return_value = True

        req = UpdateUrlRequest.model_validate({"expired_redirect_url": None})
        await svc.update(URL_OID, req, USER_OID)

        update_doc = url_repo.update.call_args[0][1]
        assert update_doc["$set"]["expired_redirect_url"] is None
        assert "secondary_hosts" not in update_doc["$set"]["dest"]

    @pytest.mark.asyncio
    async def test_update_null_on_unset_fallback_is_a_noop(self):
        svc, url_repo, _ = self._svc()
        url_repo.find_by_id.return_value = make_url_v2_doc()

        req = UpdateUrlRequest.model_validate({"expired_redirect_url": None})
        await svc.update(URL_OID, req, USER_OID)

        url_repo.update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_counts_the_fallback_in_l1(self):
        svc, _, _ = self._svc()
        svc._url_policy.record_create = AsyncMock()
        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(
            long_url="https://example.com", expired_redirect_url=self.FALLBACK
        )
        await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")

        recorded = [c.args[0] for c in svc._url_policy.record_create.await_args_list]
        assert recorded == ["https://example.com", self.FALLBACK]

    def test_cache_projection_carries_fallback(self):
        doc = make_url_v2_doc(expired_redirect_url=self.FALLBACK)
        assert UrlCacheData.from_v2_doc(doc).expired_redirect_url == self.FALLBACK

    @pytest.mark.asyncio
    async def test_cache_hit_expired_with_fallback_raises_redirect(self):
        svc, _, url_cache = self._svc()
        url_cache.get.return_value = make_active_cache(url_status="EXPIRED").model_copy(
            update={"expired_redirect_url": self.FALLBACK}
        )

        with pytest.raises(ExpiredRedirectError) as exc:
            await svc.resolve(ALIAS)
        assert exc.value.redirect_url == self.FALLBACK
        assert isinstance(exc.value, GoneError)

    @pytest.mark.asyncio
    async def test_inactive_never_uses_the_fallback(self):
        svc, _, url_cache = self._svc()
        url_cache.get.return_value = make_active_cache(
            url_status="INACTIVE"
        ).model_copy(update={"expired_redirect_url": self.FALLBACK})

        with pytest.raises(GoneError) as exc:
            await svc.resolve(ALIAS)
        assert not isinstance(exc.value, ExpiredRedirectError)

    @pytest.mark.asyncio
    async def test_time_expiry_with_fallback_flips_then_redirects(self):
        svc, url_repo, url_cache = self._svc()
        url_cache.get.return_value = make_active_cache(
            expiration_time=int(time_module.time()) - 60
        ).model_copy(update={"expired_redirect_url": self.FALLBACK})
        url_repo.expire_if_time_reached.return_value = True

        with pytest.raises(ExpiredRedirectError) as exc:
            await svc.resolve(ALIAS)
        assert exc.value.redirect_url == self.FALLBACK
        url_repo.expire_if_time_reached.assert_awaited_once_with(URL_OID)
        url_cache.invalidate.assert_awaited_once_with(ALIAS, SYSTEM_DEFAULT_DOMAIN)

    @pytest.mark.asyncio
    async def test_db_path_expired_with_fallback_raises_redirect(self):
        svc, url_repo, url_cache = self._svc()
        url_cache.get.return_value = None
        url_repo.find_by_alias.return_value = make_url_v2_doc(
            status="EXPIRED", expired_redirect_url=self.FALLBACK
        )

        with pytest.raises(ExpiredRedirectError) as exc:
            await svc.resolve(ALIAS)
        assert exc.value.redirect_url == self.FALLBACK
        url_cache.set.assert_awaited_once()


class TestUrlServiceGeoRules:
    """Geo-targeting: create/update validation, persistence, cache projection."""

    GEO: typing.ClassVar[dict] = {
        "IN": "https://example.in/",
        "US": "https://example.com/us",
    }

    @pytest.mark.asyncio
    async def test_create_persists_geo_rules(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        blocked_url_repo.get_patterns.return_value = []
        url_repo.check_alias_exists.return_value = False
        url_repo.insert.return_value = URL_OID

        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(long_url="https://example.com", geo_rules=self.GEO)
        result, _claim_token = await svc.create(
            req, owner_id=USER_OID, client_ip="1.2.3.4"
        )

        assert result.geo_rules == self.GEO
        inserted = url_repo.insert.call_args[0][0]
        assert inserted["geo_rules"] == self.GEO

    @pytest.mark.asyncio
    async def test_create_normalises_lowercase_country_codes(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        blocked_url_repo.get_patterns.return_value = []
        url_repo.check_alias_exists.return_value = False
        url_repo.insert.return_value = URL_OID

        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(
            long_url="https://example.com",
            geo_rules={" in ": "https://example.in/"},
        )
        result, _claim_token = await svc.create(
            req, owner_id=USER_OID, client_ip="1.2.3.4"
        )
        assert result.geo_rules == {"IN": "https://example.in/"}

    @pytest.mark.asyncio
    async def test_create_invalid_iso_code_rejected_with_field_path(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        blocked_url_repo.get_patterns.return_value = []

        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(
            long_url="https://example.com",
            geo_rules={"XX": "https://example.com/xx"},
        )
        with pytest.raises(ValidationError) as exc:
            await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")
        assert exc.value.field == "geo_rules.XX"

    def test_too_many_entries_rejected(self):
        """Entry cap comes from settings (geo_rules_max_countries) and is
        enforced in the service validator — same split as
        max_emoji_alias_length."""
        import pycountry

        from app.services.url_service import _validate_geo_rules_shape

        codes = [c.alpha_2 for c in pycountry.countries][:51]
        rules = {code: "https://example.com/x" for code in codes}

        with pytest.raises(ValidationError) as exc:
            _validate_geo_rules_shape(rules, max_countries=50)
        assert exc.value.field == "geo_rules"

    @pytest.mark.asyncio
    async def test_create_blocked_geo_destination_rejected(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        blocked_url_repo.get_patterns.return_value = [r"https://evil\.com"]

        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(
            long_url="https://example.com",
            geo_rules={"IN": "https://evil.com/page"},
        )
        with pytest.raises(ValidationError) as exc:
            await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")
        assert exc.value.field == "geo_rules.IN"
        assert "blocked" in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_create_self_link_geo_destination_rejected(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        blocked_url_repo.get_patterns.return_value = []

        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(
            long_url="https://example.com",
            geo_rules={"IN": "https://spoo.me/other"},
        )
        with pytest.raises(ValidationError) as exc:
            await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")
        assert exc.value.field == "geo_rules.IN"

    @pytest.mark.asyncio
    async def test_create_fetches_blocklist_patterns_once(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        blocked_url_repo.get_patterns.return_value = []
        url_repo.check_alias_exists.return_value = False
        url_repo.insert.return_value = URL_OID

        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(
            long_url="https://example.com",
            geo_rules={
                "IN": "https://example.in/",
                "US": "https://example.com/us",
                "DE": "https://example.de/",
            },
        )
        await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")
        # One pattern fetch for long_url plus one per geo destination (ttl=0 here).
        assert blocked_url_repo.get_patterns.await_count == 4

    @pytest.mark.asyncio
    async def test_update_sets_geo_rules(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        existing = make_url_v2_doc()
        url_repo.find_by_id.return_value = existing
        url_repo.update.return_value = True
        blocked_url_repo.get_patterns.return_value = []

        from app.schemas.dto.requests.url import UpdateUrlRequest

        req = UpdateUrlRequest(geo_rules=self.GEO)
        await svc.update(URL_OID, req, USER_OID)

        update_doc = url_repo.update.call_args[0][1]
        assert update_doc["$set"]["geo_rules"] == self.GEO
        url_cache.invalidate.assert_called_once_with(ALIAS, SYSTEM_DEFAULT_DOMAIN)

    @pytest.mark.asyncio
    async def test_update_null_clears_geo_rules(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        existing = make_url_v2_doc(geo_rules=self.GEO)
        url_repo.find_by_id.return_value = existing
        url_repo.update.return_value = True

        from app.schemas.dto.requests.url import UpdateUrlRequest

        # Explicit null must be present in model_fields_set — build via validate
        req = UpdateUrlRequest.model_validate({"geo_rules": None})
        assert "geo_rules" in req.model_fields_set
        await svc.update(URL_OID, req, USER_OID)

        update_doc = url_repo.update.call_args[0][1]
        assert update_doc["$set"]["geo_rules"] is None

    @pytest.mark.asyncio
    async def test_update_empty_map_clears_geo_rules(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        existing = make_url_v2_doc(geo_rules=self.GEO)
        url_repo.find_by_id.return_value = existing
        url_repo.update.return_value = True

        from app.schemas.dto.requests.url import UpdateUrlRequest

        req = UpdateUrlRequest.model_validate({"geo_rules": {}})
        await svc.update(URL_OID, req, USER_OID)

        update_doc = url_repo.update.call_args[0][1]
        assert update_doc["$set"]["geo_rules"] is None

    @pytest.mark.asyncio
    async def test_update_omitted_geo_rules_untouched(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        existing = make_url_v2_doc(geo_rules=self.GEO)
        url_repo.find_by_id.return_value = existing
        url_repo.update.return_value = True
        blocked_url_repo.get_patterns.return_value = []

        from app.schemas.dto.requests.url import UpdateUrlRequest

        req = UpdateUrlRequest(long_url="https://new-url.com")
        await svc.update(URL_OID, req, USER_OID)

        update_doc = url_repo.update.call_args[0][1]
        assert "geo_rules" not in update_doc["$set"]

    @pytest.mark.asyncio
    async def test_update_identical_geo_rules_is_noop_and_skips_validation(self):
        """A read-modify-write PATCH echoing unchanged rules must not fail
        even if a destination entered the blocklist since creation — the
        changed-check runs BEFORE validation, like every other handler."""
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        existing = make_url_v2_doc(geo_rules=self.GEO)
        url_repo.find_by_id.return_value = existing
        # Would reject every GEO destination if validation ran
        blocked_url_repo.get_patterns.return_value = [r"https://example\."]

        from app.schemas.dto.requests.url import UpdateUrlRequest

        req = UpdateUrlRequest(geo_rules=self.GEO)
        await svc.update(URL_OID, req, USER_OID)

        url_repo.update.assert_not_called()
        blocked_url_repo.get_patterns.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_update_blocked_geo_destination_rejected(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        existing = make_url_v2_doc()
        url_repo.find_by_id.return_value = existing
        blocked_url_repo.get_patterns.return_value = [r"https://evil\.com"]

        from app.schemas.dto.requests.url import UpdateUrlRequest

        req = UpdateUrlRequest(geo_rules={"IN": "https://evil.com/x"})
        with pytest.raises(ValidationError) as exc:
            await svc.update(URL_OID, req, USER_OID)
        assert exc.value.field == "geo_rules.IN"

    @pytest.mark.asyncio
    async def test_update_long_url_now_rejects_blocklisted_destination(self):
        """The pre-existing edit-path gap: create checked the DB blocklist but
        update didn't. An edit must not be a side door for blocked URLs."""
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        existing = make_url_v2_doc()
        url_repo.find_by_id.return_value = existing
        blocked_url_repo.get_patterns.return_value = [r"https://evil\.com"]

        from app.schemas.dto.requests.url import UpdateUrlRequest

        req = UpdateUrlRequest(long_url="https://evil.com/page")
        with pytest.raises(ValidationError) as exc:
            await svc.update(URL_OID, req, USER_OID)
        assert exc.value.field == "long_url"
        assert "blocked" in str(exc.value).lower()

    def test_v2_doc_to_cache_carries_geo_rules(self):
        from app.infrastructure.cache.url_cache import UrlCacheData

        doc = make_url_v2_doc(geo_rules=self.GEO)
        cache_data = UrlCacheData.from_v2_doc(doc)
        assert cache_data.geo_rules == self.GEO

    def test_v2_doc_to_cache_none_when_no_rules(self):
        from app.infrastructure.cache.url_cache import UrlCacheData

        doc = make_url_v2_doc()
        cache_data = UrlCacheData.from_v2_doc(doc)
        assert cache_data.geo_rules is None

    @pytest.mark.asyncio
    async def test_resolve_cache_hit_preserves_geo_rules(self):
        """The named 'easiest bug to ship': geo rules must survive the cache
        projection — a cache-hit resolve must still see them."""
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        # Miss → DB → populate cache
        url_cache.get.return_value = None
        url_repo.find_by_alias.return_value = make_url_v2_doc(geo_rules=self.GEO)
        data, _schema = await svc.resolve(ALIAS)
        assert data.geo_rules == self.GEO

        # The populated cache entry carries the rules
        cached = url_cache.set.call_args[0][1]
        assert cached.geo_rules == self.GEO

        # Hit → served straight from cache, rules intact
        url_cache.get.return_value = cached
        url_repo.find_by_alias.reset_mock()
        data2, _ = await svc.resolve(ALIAS)
        assert data2.geo_rules == self.GEO
        url_repo.find_by_alias.assert_not_called()


# TestUrlServiceAbVariants
# ─────────────────────────────────────────────────────────────────────────────


class TestAbVariantsFeatureGate:
    @pytest.mark.asyncio
    async def test_ab_variants_refused_while_the_feature_is_dark(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo,
            legacy_repo,
            emoji_repo,
            blocked_url_repo,
            url_cache,
            ab_variants_enabled=False,
        )
        blocked_url_repo.get_patterns.return_value = []

        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(
            long_url="https://example.com",
            ab_variants=[{"url": "https://example.com/b", "weight": 40}],
        )
        with pytest.raises(ValidationError) as exc:
            await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")
        assert exc.value.field == "ab_variants"
        url_repo.insert.assert_not_awaited()


class TestUrlServiceAbVariants:
    """A/B variants: create/update validation, persistence, cache projection."""

    VARIANTS: typing.ClassVar[list[dict]] = [
        {"url": "https://example.com/b", "weight": 60},
        {"url": "https://example.com/c", "weight": 40},
    ]

    @pytest.mark.asyncio
    async def test_create_persists_ab_variants(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        blocked_url_repo.get_patterns.return_value = []
        url_repo.check_alias_exists.return_value = False
        url_repo.insert.return_value = URL_OID

        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(
            long_url="https://example.com", ab_variants=self.VARIANTS
        )
        result, _token = await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")

        assert [v.model_dump() for v in result.ab_variants] == self.VARIANTS
        inserted = url_repo.insert.call_args[0][0]
        assert inserted["ab_variants"] == self.VARIANTS

    @pytest.mark.asyncio
    async def test_create_without_variants_stores_none(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        blocked_url_repo.get_patterns.return_value = []
        url_repo.check_alias_exists.return_value = False
        url_repo.insert.return_value = URL_OID

        from app.schemas.dto.requests.url import CreateUrlRequest

        await svc.create(
            CreateUrlRequest(long_url="https://example.com", ab_variants=[]),
            owner_id=USER_OID,
            client_ip="1.2.3.4",
        )
        assert url_repo.insert.call_args[0][0]["ab_variants"] is None

    def test_too_many_entries_rejected(self):
        from app.schemas.dto.requests.url import AbVariantRequest
        from app.services.url_service import _validate_ab_variants_shape

        variants = [
            AbVariantRequest(url="https://example.com/x", weight=1) for _ in range(11)
        ]
        with pytest.raises(ValidationError) as exc:
            _validate_ab_variants_shape(variants, max_variants=10)
        assert exc.value.field == "ab_variants"

    @pytest.mark.asyncio
    async def test_create_blocked_variant_destination_rejected(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        blocked_url_repo.get_patterns.return_value = [r"https://evil\.com"]

        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(
            long_url="https://example.com",
            ab_variants=[
                {"url": "https://example.com/b", "weight": 30},
                {"url": "https://evil.com/page", "weight": 30},
            ],
        )
        with pytest.raises(ValidationError) as exc:
            await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")
        assert exc.value.field == "ab_variants.1.url"
        assert "blocked" in str(exc.value).lower()
        url_repo.insert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_self_link_variant_rejected(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        blocked_url_repo.get_patterns.return_value = []

        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(
            long_url="https://example.com",
            ab_variants=[{"url": "https://spoo.me/other", "weight": 50}],
        )
        with pytest.raises(ValidationError) as exc:
            await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")
        assert exc.value.field == "ab_variants.0.url"

    @pytest.mark.asyncio
    async def test_update_sets_ab_variants(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        existing = make_url_v2_doc()
        url_repo.find_by_id.return_value = existing
        url_repo.update.return_value = make_url_v2_doc(ab_variants=self.VARIANTS)
        blocked_url_repo.get_patterns.return_value = []

        from app.schemas.dto.requests.url import UpdateUrlRequest

        await svc.update(URL_OID, UpdateUrlRequest(ab_variants=self.VARIANTS), USER_OID)

        update_doc = url_repo.update.call_args[0][1]
        assert update_doc["$set"]["ab_variants"] == self.VARIANTS

    @pytest.mark.asyncio
    @pytest.mark.parametrize("clear_value", [None, []])
    async def test_update_null_or_empty_clears_ab_variants(self, clear_value):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        existing = make_url_v2_doc(ab_variants=self.VARIANTS)
        url_repo.find_by_id.return_value = existing
        url_repo.update.return_value = make_url_v2_doc()

        from app.schemas.dto.requests.url import UpdateUrlRequest

        req = UpdateUrlRequest.model_validate({"ab_variants": clear_value})
        await svc.update(URL_OID, req, USER_OID)

        update_doc = url_repo.update.call_args[0][1]
        assert update_doc["$set"]["ab_variants"] is None

    @pytest.mark.asyncio
    async def test_update_omitted_ab_variants_untouched(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        existing = make_url_v2_doc(ab_variants=self.VARIANTS)
        url_repo.find_by_id.return_value = existing
        url_repo.update.return_value = existing

        from app.schemas.dto.requests.url import UpdateUrlRequest

        await svc.update(URL_OID, UpdateUrlRequest(block_bots=True), USER_OID)

        update_doc = url_repo.update.call_args[0][1]
        assert "ab_variants" not in update_doc["$set"]

    @pytest.mark.asyncio
    async def test_update_identical_variants_is_noop_and_skips_validation(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        url_repo.find_by_id.return_value = make_url_v2_doc(ab_variants=self.VARIANTS)
        blocked_url_repo.get_patterns.return_value = [r"https://example\."]

        from app.schemas.dto.requests.url import UpdateUrlRequest

        await svc.update(URL_OID, UpdateUrlRequest(ab_variants=self.VARIANTS), USER_OID)

        url_repo.update.assert_not_called()
        blocked_url_repo.get_patterns.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_update_blocked_variant_destination_rejected(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        url_repo.find_by_id.return_value = make_url_v2_doc()
        blocked_url_repo.get_patterns.return_value = [r"https://evil\.com"]

        from app.schemas.dto.requests.url import UpdateUrlRequest

        req = UpdateUrlRequest(
            ab_variants=[{"url": "https://evil.com/x", "weight": 10}]
        )
        with pytest.raises(ValidationError) as exc:
            await svc.update(URL_OID, req, USER_OID)
        assert exc.value.field == "ab_variants.0.url"

    @pytest.mark.asyncio
    async def test_update_refused_while_the_feature_is_dark(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo,
            legacy_repo,
            emoji_repo,
            blocked_url_repo,
            url_cache,
            ab_variants_enabled=False,
        )
        url_repo.find_by_id.return_value = make_url_v2_doc()

        from app.schemas.dto.requests.url import UpdateUrlRequest

        with pytest.raises(ValidationError) as exc:
            await svc.update(
                URL_OID, UpdateUrlRequest(ab_variants=self.VARIANTS), USER_OID
            )
        assert exc.value.field == "ab_variants"

    def test_v2_doc_to_cache_carries_ab_variants(self):
        from app.infrastructure.cache.url_cache import UrlCacheData

        cache_data = UrlCacheData.from_v2_doc(
            make_url_v2_doc(ab_variants=self.VARIANTS)
        )
        assert [v.model_dump() for v in cache_data.ab_variants] == self.VARIANTS
        assert UrlCacheData.from_v2_doc(make_url_v2_doc()).ab_variants is None


# _v2_doc_to_cache — meta_tags mapping
# ─────────────────────────────────────────────────────────────────────────────


class TestV2DocToCacheMetaTags:
    def test_carries_meta_tags(self):
        from app.infrastructure.cache.url_cache import UrlCacheData

        doc = make_url_v2_doc(
            meta_tags={
                "title": "T",
                "description": "D",
                "image": "https://x.com/i.png",
                "color": "#112233",
            }
        )
        d = UrlCacheData.from_v2_doc(doc)
        assert (d.meta_title, d.meta_description, d.meta_image, d.meta_color) == (
            "T",
            "D",
            "https://x.com/i.png",
            "#112233",
        )
        assert d.meta_image_width is None
        assert d.meta_image_height is None

    def test_no_meta_tags_maps_none(self):
        from app.infrastructure.cache.url_cache import UrlCacheData

        d = UrlCacheData.from_v2_doc(make_url_v2_doc())
        assert d.meta_title is None
        assert d.meta_description is None
        assert d.meta_image is None
        assert d.meta_color is None


# ─────────────────────────────────────────────────────────────────────────────
# meta_tags — update handler + abuse validation
# ─────────────────────────────────────────────────────────────────────────────


def _meta_req(**meta):
    from app.schemas.dto.requests.url import MetaTagsRequest, UpdateUrlRequest

    if meta.get("meta_tags") is None and "meta_tags" in meta:
        return UpdateUrlRequest(meta_tags=None)
    return UpdateUrlRequest(meta_tags=MetaTagsRequest(**meta))


def _mock_meta_service() -> AsyncMock:
    """Service mock for _handle_meta_tags: image resolution passes through."""
    svc = AsyncMock()
    svc.resolve_meta_image = AsyncMock(side_effect=lambda meta, owner: (meta, None))
    return svc


class TestHandleMetaTags:
    @pytest.mark.asyncio
    async def test_absent_field_is_noop(self):
        from app.schemas.dto.requests.url import UpdateUrlRequest
        from app.services.url_service import _handle_meta_tags

        svc = AsyncMock()
        ops: dict = {}
        await _handle_meta_tags(
            UpdateUrlRequest(), make_url_v2_doc(meta_tags={"title": "T"}), ops, svc
        )
        assert ops == {}
        svc.validate_meta_tags.assert_not_called()

    @pytest.mark.asyncio
    async def test_null_clears_existing(self):
        from app.services.url_service import _handle_meta_tags

        svc = AsyncMock()
        ops: dict = {}
        await _handle_meta_tags(
            _meta_req(meta_tags=None),
            make_url_v2_doc(meta_tags={"title": "T"}),
            ops,
            svc,
        )
        assert ops == {"meta_tags": None}
        svc.validate_meta_tags.assert_not_called()

    @pytest.mark.asyncio
    async def test_null_on_link_without_meta_is_noop(self):
        from app.services.url_service import _handle_meta_tags

        svc = AsyncMock()
        ops: dict = {}
        await _handle_meta_tags(_meta_req(meta_tags=None), make_url_v2_doc(), ops, svc)
        assert ops == {}

    @pytest.mark.asyncio
    async def test_object_replaces_whole_and_stamps_updated_at(self):
        from app.services.url_service import _handle_meta_tags

        svc = _mock_meta_service()
        ops: dict = {}
        await _handle_meta_tags(
            _meta_req(title="New", color="#112233"), make_url_v2_doc(), ops, svc
        )
        written = ops["meta_tags"]
        assert written["title"] == "New"
        assert written["color"] == "#112233"
        assert written["description"] is None
        assert written["updated_at"] is not None
        svc.validate_meta_tags.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_validates_against_new_destination_when_long_url_changes(self):
        from app.services.url_service import _handle_meta_tags

        svc = _mock_meta_service()
        ops: dict = {"long_url": "https://new-destination.com"}
        await _handle_meta_tags(_meta_req(title="T"), make_url_v2_doc(), ops, svc)
        assert (
            svc.validate_meta_tags.call_args.kwargs["long_url"]
            == "https://new-destination.com"
        )


class TestValidateMetaTags:
    def _svc_with_patterns(self, patterns):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        blocked_url_repo.get_patterns.return_value = patterns
        return make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

    @pytest.mark.asyncio
    async def test_clean_content_passes(self):
        from app.schemas.dto.requests.url import MetaTagsRequest

        svc = self._svc_with_patterns(["evil-token"])
        await svc.validate_meta_tags(
            MetaTagsRequest(title="Nice launch"), long_url="https://example.com"
        )

    @pytest.mark.asyncio
    async def test_blocked_pattern_in_title_rejected(self):
        from app.schemas.dto.requests.url import MetaTagsRequest

        svc = self._svc_with_patterns(["evil-token"])
        with pytest.raises(ValidationError) as exc:
            await svc.validate_meta_tags(
                MetaTagsRequest(title="totally evil-token deal"),
                long_url="https://example.com",
            )
        assert exc.value.field == "meta_tags"

    @pytest.mark.asyncio
    async def test_blocked_pattern_in_image_rejected(self):
        from app.schemas.dto.requests.url import MetaTagsRequest

        svc = self._svc_with_patterns(["evil-token"])
        with pytest.raises(ValidationError):
            await svc.validate_meta_tags(
                MetaTagsRequest(title="ok", image="https://evil-token.com/x.png"),
                long_url="https://example.com",
            )

    @pytest.mark.asyncio
    async def test_destination_recheck_rejects_blocked_long_url(self):
        from app.schemas.dto.requests.url import MetaTagsRequest

        svc = self._svc_with_patterns(["evil-token"])
        with pytest.raises(ValidationError) as exc:
            await svc.validate_meta_tags(
                MetaTagsRequest(title="ok"), long_url="https://evil-token.com/login"
            )
        assert exc.value.field == "long_url"


# ─────────────────────────────────────────────────────────────────────────────
# TestUrlServiceTimeExpiry — resolve-time enforcement of expire_after
# ─────────────────────────────────────────────────────────────────────────────


class TestUrlServiceTimeExpiry:
    """Time-based expiry is enforced lazily at resolve (both cache-hit and
    DB paths); v2 additionally persists the status flip, mirroring
    max-clicks (expire_if_time_reached + invalidate)."""

    PAST_TS = int(time_module.time()) - 3600
    FUTURE_TS = int(time_module.time()) + 3600

    def _svc(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        url_repo.count_claimed.return_value = 0
        return svc, url_repo, url_cache, legacy_repo, emoji_repo

    @pytest.mark.asyncio
    async def test_cache_hit_v2_past_expiry_raises_gone_and_flips(self):
        svc, url_repo, url_cache, *_ = self._svc()
        url_cache.get.return_value = make_active_cache(expiration_time=self.PAST_TS)
        url_repo.expire_if_time_reached.return_value = True

        with pytest.raises(GoneError):
            await svc.resolve(ALIAS)

        url_repo.expire_if_time_reached.assert_awaited_once_with(URL_OID)
        url_cache.invalidate.assert_awaited_once_with(ALIAS, SYSTEM_DEFAULT_DOMAIN)

    @pytest.mark.asyncio
    async def test_cache_hit_v2_future_expiry_resolves(self):
        svc, url_repo, url_cache, *_ = self._svc()
        cached = make_active_cache(expiration_time=self.FUTURE_TS)
        url_cache.get.return_value = cached

        result, _schema = await svc.resolve(ALIAS)

        assert result is cached
        url_repo.expire_if_time_reached.assert_not_called()

    @pytest.mark.asyncio
    async def test_flip_already_done_elsewhere_skips_invalidate(self):
        """modified_count=0 (concurrent resolve won the flip) — still 410,
        but no duplicate invalidation side effect."""
        svc, url_repo, url_cache, *_ = self._svc()
        url_cache.get.return_value = make_active_cache(expiration_time=self.PAST_TS)
        url_repo.expire_if_time_reached.return_value = False

        with pytest.raises(GoneError):
            await svc.resolve(ALIAS)

        url_cache.invalidate.assert_not_called()

    @pytest.mark.asyncio
    async def test_db_path_v2_past_expiry_raises_gone_after_recache(self):
        svc, url_repo, url_cache, *_ = self._svc()
        url_cache.get.return_value = None
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        url_repo.find_by_alias.return_value = make_url_v2_doc(expire_after=past)
        url_repo.expire_if_time_reached.return_value = True

        with pytest.raises(GoneError):
            await svc.resolve(ALIAS)

        # Recache-before-raise keeps hot expired links O(1) — the entry is
        # written (step 3) before the expiry raise (step 4c).
        url_cache.set.assert_awaited_once()
        url_repo.expire_if_time_reached.assert_awaited_once_with(URL_OID)

    @pytest.mark.asyncio
    async def test_stored_blocked_wins_over_time_expiry(self):
        """Ordering: stored-status checks run first — BLOCKED must keep
        raising BlockedUrlError, and no flip may touch a BLOCKED doc."""
        svc, url_repo, url_cache, *_ = self._svc()
        url_cache.get.return_value = make_active_cache(
            expiration_time=self.PAST_TS, url_status="BLOCKED"
        )

        with pytest.raises(BlockedUrlError):
            await svc.resolve(ALIAS)

        url_repo.expire_if_time_reached.assert_not_called()

    @pytest.mark.asyncio
    async def test_stored_expired_skips_flip(self):
        svc, url_repo, url_cache, *_ = self._svc()
        url_cache.get.return_value = make_active_cache(
            expiration_time=self.PAST_TS, url_status="EXPIRED"
        )

        with pytest.raises(GoneError):
            await svc.resolve(ALIAS)

        url_repo.expire_if_time_reached.assert_not_called()

    @pytest.mark.asyncio
    async def test_cached_v1_past_expiry_raises_gone_without_flip(self):
        """v1 has no status field — lazy-raise forever, never a flip."""
        svc, url_repo, url_cache, *_ = self._svc()
        url_cache.get.return_value = make_active_cache(
            schema="v1", alias="abcdef", expiration_time=self.PAST_TS
        )

        with pytest.raises(GoneError):
            await svc.resolve("abcdef")

        url_repo.expire_if_time_reached.assert_not_called()

    @pytest.mark.asyncio
    async def test_db_emoji_past_aware_expiry_raises_gone(self):
        svc, url_repo, url_cache, _legacy_repo, emoji_repo = self._svc()
        url_cache.get.return_value = None
        url_repo.find_by_alias.return_value = None  # v2 is tried first now
        emoji_repo.find_by_id.return_value = EmojiUrlDoc.from_mongo(
            {
                "_id": "🐍🔥💎",
                "url": "https://emoji.example.com",
                "block-bots": False,
                "total-clicks": 0,
                "expiration-time": datetime.now(timezone.utc) - timedelta(hours=1),
            }
        )

        with pytest.raises(GoneError):
            await svc.resolve("🐍🔥💎")

        url_repo.expire_if_time_reached.assert_not_called()

    @pytest.mark.asyncio
    async def test_db_v1_naive_expiry_is_ambiguous_and_resolves(self):
        """tz-naive v1 expiration-time never enforces — matches
        v1_is_expired (public pages) and convert_to_gmt (legacy stats)."""
        svc, _url_repo, url_cache, legacy_repo, _emoji_repo = self._svc()
        url_cache.get.return_value = None
        naive_past = datetime(2020, 1, 1)  # naive → ambiguous
        legacy_repo.find_by_id.return_value = LegacyUrlDoc.from_mongo(
            {
                "_id": "abcdef",
                "url": "https://legacy.example.com",
                "block-bots": False,
                "total-clicks": 0,
                "expiration-time": naive_past,
            }
        )

        result, schema = await svc.resolve("abcdef")

        assert result.expiration_time is None
        assert schema == "v1"

    @pytest.mark.asyncio
    async def test_db_v1_aware_past_expiry_raises_gone(self):
        svc, _url_repo, url_cache, legacy_repo, _emoji_repo = self._svc()
        url_cache.get.return_value = None
        legacy_repo.find_by_id.return_value = LegacyUrlDoc.from_mongo(
            {
                "_id": "abcdef",
                "url": "https://legacy.example.com",
                "block-bots": False,
                "total-clicks": 0,
                "expiration-time": datetime.now(timezone.utc) - timedelta(hours=1),
            }
        )

        with pytest.raises(GoneError):
            await svc.resolve("abcdef")

    @pytest.mark.asyncio
    async def test_custom_domain_past_expiry_flips_and_invalidates_scoped_key(self):
        """Custom tenants ride the same enforcement — dispatch is v2-only,
        the flip is by _id, and the invalidation must target the
        tenant-scoped cache key (data.domain), not the system default."""
        svc, url_repo, url_cache, *_ = self._svc()
        url_cache.get.return_value = None
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        url_repo.find_by_alias.return_value = make_url_v2_doc(
            alias="customalias", expire_after=past, domain="links.acme.com"
        )
        url_repo.expire_if_time_reached.return_value = True

        with pytest.raises(GoneError):
            await svc.resolve("customalias", domain="links.acme.com")

        url_repo.find_by_alias.assert_awaited_once_with("customalias", "links.acme.com")
        url_repo.expire_if_time_reached.assert_awaited_once_with(URL_OID)
        url_cache.invalidate.assert_awaited_once_with("customalias", "links.acme.com")


# ─────────────────────────────────────────────────────────────────────────────
# TestEffectiveStatusClause — Mongo clause ≡ UrlV2Doc.effective_status
# ─────────────────────────────────────────────────────────────────────────────


def _matches_expired_clause(doc: UrlV2Doc, now: datetime) -> bool:
    """Python evaluation of effective_status_clause(EXPIRED, now) — must
    mirror _derived_expiry_arms exactly (the predicate-pin's referee)."""
    if doc.status == "EXPIRED":
        return True
    if doc.status == "ACTIVE":
        expire = doc.expire_after
        if expire is not None:
            aware = expire if expire.tzinfo else expire.replace(tzinfo=timezone.utc)
            if aware <= now:
                return True
        if doc.max_clicks is not None and doc.total_clicks >= doc.max_clicks:
            return True
    return False


class TestEffectiveStatusClause:
    def test_expired_clause_shape(self):
        from app.services.url_service import effective_status_clause

        now = datetime.now(timezone.utc)
        clause = effective_status_clause(UrlStatus.EXPIRED, now)
        assert clause == {
            "$or": [
                {"status": UrlStatus.EXPIRED},
                {"status": UrlStatus.ACTIVE, "expire_after": {"$lte": now}},
                {
                    "status": UrlStatus.ACTIVE,
                    "max_clicks": {"$ne": None},
                    "$expr": {"$gte": ["$total_clicks", "$max_clicks"]},
                },
            ]
        }

    def test_active_clause_is_complement_of_derived_arms(self):
        from app.services.url_service import (
            _derived_expiry_arms,
            effective_status_clause,
        )

        now = datetime.now(timezone.utc)
        clause = effective_status_clause(UrlStatus.ACTIVE, now)
        assert clause == {
            "status": UrlStatus.ACTIVE,
            "$nor": [
                *_derived_expiry_arms(now),
                {"status": UrlStatus.ACTIVE, "starts_at": {"$gt": now}},
            ],
        }

    @pytest.mark.parametrize("status", [UrlStatus.INACTIVE, UrlStatus.BLOCKED])
    def test_stored_is_truth_for_admin_states(self, status):
        from app.services.url_service import effective_status_clause

        clause = effective_status_clause(status, datetime.now(timezone.utc))
        assert clause == {"status": status}

    def test_predicate_pin_matrix(self):
        """effective_status == EXPIRED iff doc matches the EXPIRED clause,
        across the full status x expiry x max-clicks matrix. If this
        fails, the model property and the Mongo filter have diverged."""
        now = datetime.now(timezone.utc)
        statuses = ["ACTIVE", "INACTIVE", "EXPIRED", "BLOCKED"]
        expiries = [None, now - timedelta(hours=1), now + timedelta(hours=1)]
        clicks = [(None, 0), (5, 5), (5, 2)]

        for status in statuses:
            for expire_after in expiries:
                for max_clicks, total_clicks in clicks:
                    doc = make_url_v2_doc(
                        status=status,
                        expire_after=expire_after,
                        max_clicks=max_clicks,
                    )
                    doc.total_clicks = total_clicks
                    case = f"{status}/{expire_after}/{max_clicks}:{total_clicks}"
                    assert (doc.effective_status == UrlStatus.EXPIRED) == (
                        _matches_expired_clause(doc, now)
                    ), case


# ─────────────────────────────────────────────────────────────────────────────
# TestListFilterComposition — status/search $or clauses compose via $and
# ─────────────────────────────────────────────────────────────────────────────


class TestListFilterComposition:
    def _svc(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        url_repo.count_by_query.return_value = 0
        url_repo.find_by_owner.return_value = []
        return svc, url_repo

    @pytest.mark.asyncio
    async def test_expired_filter_includes_derived_arms(self):
        svc, url_repo = self._svc()
        from app.schemas.dto.requests.url import ListUrlsQuery

        await svc.list_by_owner(USER_OID, ListUrlsQuery(filter='{"status": "EXPIRED"}'))

        q = url_repo.count_by_query.call_args[0][0]
        assert q["owner_id"] == USER_OID
        arms = q["$or"]
        assert {"status": UrlStatus.EXPIRED} in arms
        assert any(a.get("expire_after") for a in arms)

    @pytest.mark.asyncio
    async def test_active_filter_excludes_derived_expired(self):
        svc, url_repo = self._svc()
        from app.schemas.dto.requests.url import ListUrlsQuery

        await svc.list_by_owner(USER_OID, ListUrlsQuery(filter='{"status": "ACTIVE"}'))

        q = url_repo.count_by_query.call_args[0][0]
        assert q["status"] == UrlStatus.ACTIVE
        assert "$nor" in q

    @pytest.mark.asyncio
    async def test_status_and_search_compose_via_and(self):
        """Both clauses carry $or — naive merging would clobber one."""
        svc, url_repo = self._svc()
        from app.schemas.dto.requests.url import ListUrlsQuery

        await svc.list_by_owner(
            USER_OID,
            ListUrlsQuery(filter='{"status": "EXPIRED", "search": "example"}'),
        )

        q = url_repo.count_by_query.call_args[0][0]
        assert "$or" not in q  # neither clause owns the top level
        status_clause, search_clause = None, None
        for clause in q["$and"]:
            if any("status" in arm for arm in clause["$or"]):
                status_clause = clause
            else:
                search_clause = clause
        assert status_clause is not None
        assert search_clause is not None

    @pytest.mark.asyncio
    async def test_search_only_still_merges_to_top_level(self):
        svc, url_repo = self._svc()
        from app.schemas.dto.requests.url import ListUrlsQuery

        await svc.list_by_owner(USER_OID, ListUrlsQuery(filter='{"search": "ex"}'))

        q = url_repo.count_by_query.call_args[0][0]
        assert "$or" in q
        assert "$and" not in q


# ─────────────────────────────────────────────────────────────────────────────
# TestAutoReactivateRequiresRequestedChange — §7 resurrection regressions
# ─────────────────────────────────────────────────────────────────────────────


class TestAutoReactivateRequiresRequestedChange:
    """Reactivation is driven by the REQUESTED change — ambient field
    values must not resurrect an EXPIRED URL on unrelated edits."""

    def _expired_doc(self, **overrides):
        base = {
            "_id": URL_OID,
            "alias": ALIAS,
            "owner_id": USER_OID,
            "domain": SYSTEM_DEFAULT_DOMAIN,
            "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
            "long_url": "https://example.com",
            "status": "EXPIRED",
            "private_stats": True,
        }
        base.update(overrides)
        return UrlV2Doc.from_mongo(base)

    def _svc(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        blocked_url_repo.get_patterns.return_value = []
        url_repo.update.return_value = True
        return svc, url_repo

    @pytest.mark.asyncio
    async def test_unrelated_edit_does_not_resurrect(self):
        """Max-clicks-expired doc with a future expire_after: editing only
        long_url must NOT flip it back to ACTIVE."""
        svc, url_repo = self._svc()
        url_repo.find_by_id.return_value = self._expired_doc(
            max_clicks=3,
            total_clicks=3,
            expire_after=datetime.now(timezone.utc) + timedelta(days=30),
        )

        from app.schemas.dto.requests.url import UpdateUrlRequest

        await svc.update(
            URL_OID, UpdateUrlRequest(long_url="https://other.org"), USER_OID
        )

        update_doc = url_repo.update.call_args[0][1]
        assert "status" not in update_doc["$set"]

    @pytest.mark.asyncio
    async def test_noop_update_does_not_write(self):
        svc, url_repo = self._svc()
        url_repo.find_by_id.return_value = self._expired_doc(
            max_clicks=3,
            total_clicks=3,
            expire_after=datetime.now(timezone.utc) + timedelta(days=30),
        )

        from app.schemas.dto.requests.url import UpdateUrlRequest

        result = await svc.update(URL_OID, UpdateUrlRequest(), USER_OID)

        url_repo.update.assert_not_called()
        assert result.status == UrlStatus.EXPIRED

    @pytest.mark.asyncio
    async def test_extending_expiry_on_time_flipped_doc_reactivates(self):
        """The legitimate path still works: a time-expired (flipped) doc
        whose expire_after is explicitly extended goes back to ACTIVE."""
        svc, url_repo = self._svc()
        url_repo.find_by_id.return_value = self._expired_doc(
            expire_after=datetime.now(timezone.utc) - timedelta(days=1),
        )

        from app.schemas.dto.requests.url import UpdateUrlRequest

        future = datetime.now(timezone.utc) + timedelta(days=7)
        await svc.update(URL_OID, UpdateUrlRequest(expire_after=future), USER_OID)

        update_doc = url_repo.update.call_args[0][1]
        assert update_doc["$set"]["status"] == "ACTIVE"


# ─────────────────────────────────────────────────────────────────────────────
# TestUrlServiceGetOwned
# ─────────────────────────────────────────────────────────────────────────────


class TestUrlServiceGetOwned:
    """Single-resource reads: ownership lives IN the repo query, misses and
    foreign links both surface as NotFoundError, and reads are status-blind
    for the owner."""

    @pytest.mark.asyncio
    async def test_get_owned_queries_id_and_owner_together(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        doc = make_url_v2_doc()
        url_repo.find_by_id_and_owner.return_value = doc

        result = await svc.get_owned(URL_OID, USER_OID)

        assert result is doc
        url_repo.find_by_id_and_owner.assert_awaited_once_with(URL_OID, USER_OID)
        # Never the unscoped lookup — that shape would need a post-hoc
        # ownership check and become an existence oracle.
        url_repo.find_by_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_owned_miss_raises_not_found(self):
        """Foreign and missing ids are the same repo answer (None) and the
        same error — no existence oracle."""
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        url_repo.find_by_id_and_owner.return_value = None

        with pytest.raises(NotFoundError):
            await svc.get_owned(URL_OID, USER_OID)

    @pytest.mark.asyncio
    async def test_get_owned_is_status_blind(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        doc = make_url_v2_doc(status="INACTIVE")
        url_repo.find_by_id_and_owner.return_value = doc

        result = await svc.get_owned(URL_OID, USER_OID)

        assert result.status == UrlStatus.INACTIVE

    @pytest.mark.asyncio
    async def test_get_owned_by_alias_defaults_to_system_domain(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        doc = make_url_v2_doc()
        url_repo.find_by_alias_and_owner.return_value = doc

        result = await svc.get_owned_by_alias(ALIAS, USER_OID)

        assert result is doc
        url_repo.find_by_alias_and_owner.assert_awaited_once_with(
            ALIAS, SYSTEM_DEFAULT_DOMAIN, USER_OID
        )

    @pytest.mark.asyncio
    async def test_get_owned_by_alias_scopes_to_custom_domain(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        doc = make_url_v2_doc(domain="links.acme.com")
        url_repo.find_by_alias_and_owner.return_value = doc

        await svc.get_owned_by_alias(ALIAS, USER_OID, domain="links.acme.com")

        url_repo.find_by_alias_and_owner.assert_awaited_once_with(
            ALIAS, "links.acme.com", USER_OID
        )
        # v2-only: the legacy collections are never consulted.
        legacy_repo.find_by_id.assert_not_called()
        emoji_repo.find_by_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_owned_by_alias_miss_raises_not_found(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        url_repo.find_by_alias_and_owner.return_value = None

        with pytest.raises(NotFoundError):
            await svc.get_owned_by_alias(ALIAS, USER_OID)


# ─────────────────────────────────────────────────────────────────────────────
# Single-item edge purge (takedown parity with the bulk endpoints)
# ─────────────────────────────────────────────────────────────────────────────


class TestSingleItemEdgePurge:
    """Plain links' hot-promoted KV entries are purged on takedown-shaped
    mutations, mirroring the bulk endpoints. og-links stay event-managed
    by the write-through (no double-fire)."""

    def _service_with_edge(self, url_repo, og=None):
        edge_kv = MagicMock()
        edge_kv.delete = AsyncMock(return_value=True)
        svc = make_service(
            url_repo,
            AsyncMock(),
            AsyncMock(),
            AsyncMock(),
            AsyncMock(),
            og_writethrough=og,
            edge_kv=edge_kv,
        )
        return svc, edge_kv

    async def _drain(self, svc):
        import asyncio

        while svc._edge_purge_tasks:
            await asyncio.gather(*list(svc._edge_purge_tasks))

    @pytest.mark.asyncio
    async def test_delete_plain_link_purges_edge_key(self):
        url_repo = AsyncMock()
        url_repo.find_by_id.return_value = make_url_v2_doc()
        svc, edge_kv = self._service_with_edge(url_repo)

        await svc.delete(URL_OID, USER_OID)
        await self._drain(svc)

        edge_kv.delete.assert_awaited_once_with(
            f"cache:{SYSTEM_DEFAULT_DOMAIN}:{ALIAS}"
        )

    @pytest.mark.asyncio
    async def test_update_start_time_on_plain_link_purges_edge_key(self):
        """Pushing starts_at into the future takes the link dark at origin;
        a promoted entry must not keep serving the redirect for its TTL."""
        url_repo = AsyncMock()
        url_repo.find_by_id.return_value = make_url_v2_doc()
        url_repo.update.return_value = True
        svc, edge_kv = self._service_with_edge(url_repo)

        future = int(time_module.time()) + 3600
        await svc.update(URL_OID, UpdateUrlRequest(starts_at=future), USER_OID)
        await self._drain(svc)

        edge_kv.delete.assert_awaited_once_with(
            f"cache:{SYSTEM_DEFAULT_DOMAIN}:{ALIAS}"
        )

    @pytest.mark.asyncio
    async def test_update_start_time_on_og_link_resyncs_writethrough(self):
        url_repo = AsyncMock()
        url_repo.find_by_id.return_value = make_url_v2_doc(meta_tags={"title": "T"})
        url_repo.update.return_value = True
        og = MagicMock(remove=AsyncMock(), sync=AsyncMock())
        svc, edge_kv = self._service_with_edge(url_repo, og=og)

        future = int(time_module.time()) + 3600
        await svc.update(URL_OID, UpdateUrlRequest(starts_at=future), USER_OID)
        await self._drain(svc)

        og.sync.assert_awaited_once()
        edge_kv.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delete_og_link_uses_writethrough_not_purge(self):
        url_repo = AsyncMock()
        url_repo.find_by_id.return_value = make_url_v2_doc(meta_tags={"title": "T"})
        og = MagicMock(remove=AsyncMock(), sync=AsyncMock())
        svc, edge_kv = self._service_with_edge(url_repo, og=og)

        await svc.delete(URL_OID, USER_OID)
        await self._drain(svc)

        og.remove.assert_awaited_once_with(SYSTEM_DEFAULT_DOMAIN, ALIAS)
        edge_kv.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delete_tenant_link_never_purges(self):
        url_repo = AsyncMock()
        url_repo.find_by_id.return_value = make_url_v2_doc(domain="links.acme.com")
        svc, edge_kv = self._service_with_edge(url_repo)

        await svc.delete(URL_OID, USER_OID)
        await self._drain(svc)

        edge_kv.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_deactivate_plain_link_purges(self):
        url_repo = AsyncMock()
        url_repo.find_by_id.return_value = make_url_v2_doc()
        svc, edge_kv = self._service_with_edge(url_repo)

        await svc.update(URL_OID, UpdateUrlRequest(status=UrlStatus.INACTIVE), USER_OID)
        await self._drain(svc)

        edge_kv.delete.assert_awaited_once_with(
            f"cache:{SYSTEM_DEFAULT_DOMAIN}:{ALIAS}"
        )

    @pytest.mark.asyncio
    async def test_activate_plain_link_does_not_purge(self):
        url_repo = AsyncMock()
        url_repo.find_by_id.return_value = make_url_v2_doc(status="INACTIVE")
        svc, edge_kv = self._service_with_edge(url_repo)

        await svc.update(URL_OID, UpdateUrlRequest(status=UrlStatus.ACTIVE), USER_OID)
        await self._drain(svc)

        edge_kv.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rename_plain_link_purges_old_key(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        url_repo.find_by_id.return_value = make_url_v2_doc()
        url_repo.check_alias_exists.return_value = False  # new alias free
        legacy_repo.check_exists.return_value = False
        edge_kv = MagicMock()
        edge_kv.delete = AsyncMock(return_value=True)
        svc = make_service(
            url_repo,
            legacy_repo,
            emoji_repo,
            blocked_url_repo,
            url_cache,
            edge_kv=edge_kv,
        )

        await svc.update(URL_OID, UpdateUrlRequest(alias="newname"), USER_OID)
        await self._drain(svc)

        edge_kv.delete.assert_awaited_once_with(
            f"cache:{SYSTEM_DEFAULT_DOMAIN}:{ALIAS}"
        )

    @pytest.mark.asyncio
    async def test_expiry_only_update_does_not_purge(self):
        url_repo = AsyncMock()
        url_repo.find_by_id.return_value = make_url_v2_doc()
        svc, edge_kv = self._service_with_edge(url_repo)

        future = datetime.now(timezone.utc) + timedelta(days=7)
        await svc.update(URL_OID, UpdateUrlRequest(expire_after=future), USER_OID)
        await self._drain(svc)

        edge_kv.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_edge_kv_is_a_clean_noop(self):
        url_repo = AsyncMock()
        url_repo.find_by_id.return_value = make_url_v2_doc()
        svc = make_service(url_repo, AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock())

        await svc.delete(URL_OID, USER_OID)  # must not raise

        assert not svc._edge_purge_tasks


# ─────────────────────────────────────────────────────────────────────────────
# Claim Links
# ─────────────────────────────────────────────────────────────────────────────


def make_claim_item(url_id: ObjectId | None = None, token: str = "t" * 43):
    from app.schemas.dto.requests.url import ClaimItemRequest

    return ClaimItemRequest(url_id=str(url_id or URL_OID), token=token)


class TestUrlServiceCreateClaimToken:
    async def _create(self, owner_id):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        blocked_url_repo.get_patterns.return_value = []
        url_repo.check_alias_exists.return_value = False
        url_repo.insert.return_value = URL_OID

        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(long_url="https://example.com")
        doc, token = await svc.create(req, owner_id=owner_id, client_ip="1.2.3.4")
        return doc, token, url_repo.insert.call_args.args[0]

    @pytest.mark.asyncio
    async def test_anonymous_create_mints_token_and_stores_only_hash(self):
        from app.infrastructure.crypto import hash_token

        _doc, token, inserted = await self._create(None)
        assert token is not None
        assert len(token) >= 43  # 32 urlsafe bytes
        assert inserted["claim_token_hash"] == hash_token(token)
        assert token not in str(inserted)  # plaintext never persisted

    @pytest.mark.asyncio
    async def test_anonymous_create_strips_unset_claimed_at(self):
        # The owner_claimed partial index filters on $exists, which matches
        # nulls — the field must be absent, not null.
        _, _, inserted = await self._create(None)
        assert "claimed_at" not in inserted

    @pytest.mark.asyncio
    async def test_authed_create_mints_nothing(self):
        _, token, inserted = await self._create(USER_OID)
        assert token is None
        assert "claim_token_hash" not in inserted
        assert "claimed_at" not in inserted

    @pytest.mark.asyncio
    async def test_tokens_unique_per_create(self):
        _, t1, _ = await self._create(None)
        _, t2, _ = await self._create(None)
        assert t1 != t2


class TestUrlServiceClaim:
    TOKEN = "correct-token-" + "a" * 29  # 43 chars, the issued size

    def _svc(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        url_repo.count_claimed.return_value = 0
        return svc, url_repo, url_cache

    def _anon_doc(self, token: str | None = None):
        from app.infrastructure.crypto import hash_token

        doc = make_url_v2_doc(owner_id=ANONYMOUS_OWNER_ID)
        doc.claim_token_hash = hash_token(token or self.TOKEN)
        return doc

    @pytest.mark.asyncio
    async def test_happy_path_claims_and_evicts_cache(self):
        from app.infrastructure.crypto import hash_token

        svc, url_repo, url_cache = self._svc()
        url_repo.find_by_id.return_value = self._anon_doc()
        url_repo.claim_by_token_hash.return_value = True

        results = await svc.claim([make_claim_item(token=self.TOKEN)], USER_OID)

        assert [r.status for r in results] == ["claimed"]
        args = url_repo.claim_by_token_hash.call_args.args
        assert args[0] == URL_OID
        assert args[1] == hash_token(self.TOKEN)
        assert args[2] == USER_OID
        url_cache.invalidate.assert_awaited_once_with(ALIAS, SYSTEM_DEFAULT_DOMAIN)

    @pytest.mark.asyncio
    async def test_wrong_token_is_invalid_and_never_writes(self):
        svc, url_repo, url_cache = self._svc()
        url_repo.find_by_id.return_value = self._anon_doc()

        results = await svc.claim([make_claim_item(token="w" * 43)], USER_OID)

        assert [r.status for r in results] == ["invalid"]
        url_repo.claim_by_token_hash.assert_not_awaited()
        url_cache.invalidate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_url_is_invalid(self):
        svc, url_repo, _ = self._svc()
        url_repo.find_by_id.return_value = None

        results = await svc.claim([make_claim_item()], USER_OID)
        assert [r.status for r in results] == ["invalid"]

    @pytest.mark.asyncio
    async def test_own_link_is_already_yours_without_token_check(self):
        svc, url_repo, _ = self._svc()
        url_repo.find_by_id.return_value = make_url_v2_doc(owner_id=USER_OID)

        results = await svc.claim([make_claim_item(token="garbage" * 8)], USER_OID)

        assert [r.status for r in results] == ["already_yours"]
        url_repo.claim_by_token_hash.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_foreign_owned_link_indistinguishable_from_unknown(self):
        svc, url_repo, _ = self._svc()
        other = ObjectId("cccccccccccccccccccccccc")
        doc = make_url_v2_doc(owner_id=other)
        doc.claim_token_hash = "somehash"
        url_repo.find_by_id.return_value = doc

        results = await svc.claim([make_claim_item(token=self.TOKEN)], USER_OID)
        assert [r.status for r in results] == ["invalid"]
        url_repo.claim_by_token_hash.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pre_feature_link_without_hash_is_invalid(self):
        svc, url_repo, _ = self._svc()
        url_repo.find_by_id.return_value = make_url_v2_doc(owner_id=ANONYMOUS_OWNER_ID)

        results = await svc.claim([make_claim_item(token=self.TOKEN)], USER_OID)
        assert [r.status for r in results] == ["invalid"]

    @pytest.mark.asyncio
    async def test_lost_cas_race_is_invalid_and_no_eviction(self):
        svc, url_repo, url_cache = self._svc()
        url_repo.find_by_id.return_value = self._anon_doc()
        url_repo.claim_by_token_hash.return_value = False

        results = await svc.claim([make_claim_item(token=self.TOKEN)], USER_OID)

        assert [r.status for r in results] == ["invalid"]
        url_cache.invalidate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ceiling_rejects_batch_before_any_lookup(self):
        from app.errors import ForbiddenError
        from app.services.url_service import CLAIM_LIMIT_PER_ACCOUNT

        svc, url_repo, _ = self._svc()
        url_repo.count_claimed.return_value = CLAIM_LIMIT_PER_ACCOUNT

        with pytest.raises(ForbiddenError):
            await svc.claim([make_claim_item(token=self.TOKEN)], USER_OID)
        url_repo.find_by_id.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ceiling_allows_batch_that_exactly_fills(self):
        from app.services.url_service import CLAIM_LIMIT_PER_ACCOUNT

        svc, url_repo, _ = self._svc()
        url_repo.count_claimed.return_value = CLAIM_LIMIT_PER_ACCOUNT - 1
        url_repo.find_by_id.return_value = self._anon_doc()
        url_repo.claim_by_token_hash.return_value = True

        results = await svc.claim([make_claim_item(token=self.TOKEN)], USER_OID)
        assert [r.status for r in results] == ["claimed"]

    @pytest.mark.asyncio
    async def test_mixed_batch_preserves_order(self):
        svc, url_repo, _ = self._svc()
        unknown_id = ObjectId("dddddddddddddddddddddddd")
        mine_id = ObjectId("eeeeeeeeeeeeeeeeeeeeeeee")
        mine = make_url_v2_doc(url_id=mine_id, owner_id=USER_OID)

        def lookup(url_id):
            return {URL_OID: self._anon_doc(), mine_id: mine}.get(url_id)

        url_repo.find_by_id.side_effect = lookup
        url_repo.claim_by_token_hash.return_value = True

        results = await svc.claim(
            [
                make_claim_item(unknown_id),
                make_claim_item(mine_id),
                make_claim_item(URL_OID, token=self.TOKEN),
            ],
            USER_OID,
        )
        assert [(r.url_id, r.status) for r in results] == [
            (str(unknown_id), "invalid"),
            (str(mine_id), "already_yours"),
            (str(URL_OID), "claimed"),
        ]


# ─────────────────────────────────────────────────────────────────────────────
# TestUrlServiceDeleteAllByOwner (account erasure)
# ─────────────────────────────────────────────────────────────────────────────


def _owner_iter(docs):
    """iter_by_owner is an async generator, not a coroutine — wire a
    MagicMock returning one."""

    async def gen(_owner_id):
        for d in docs:
            yield d

    return MagicMock(side_effect=gen)


class TestUrlServiceDeleteAllByOwner:
    @pytest.mark.asyncio
    async def test_erases_across_domains_with_per_link_side_effects(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        plain = make_url_v2_doc()
        tenant = make_url_v2_doc(alias="xyz9876", domain="links.acme.com")
        url_repo.iter_by_owner = _owner_iter([plain, tenant])
        url_repo.scrub_blocked_owner_pii = AsyncMock(return_value=0)
        url_repo.delete_by_owner = AsyncMock(return_value=2)
        edge_kv = MagicMock()
        edge_kv.delete = AsyncMock(return_value=True)
        svc = make_service(
            url_repo,
            legacy_repo,
            emoji_repo,
            blocked_url_repo,
            url_cache,
            edge_kv=edge_kv,
        )

        result = await svc.delete_all_by_owner(USER_OID)
        while svc._edge_purge_tasks:
            await asyncio.gather(*list(svc._edge_purge_tasks))

        assert result.deleted == 2
        assert result.blocked_retained == 0
        assert result.url_ids == [plain.id, tenant.id]
        url_repo.delete_by_owner.assert_awaited_once_with(USER_OID)
        invalidated = [c.args for c in url_cache.invalidate.await_args_list]
        assert invalidated == [
            (ALIAS, SYSTEM_DEFAULT_DOMAIN),
            ("xyz9876", "links.acme.com"),
        ]
        # Only the system-default plain link gets an edge-KV purge.
        edge_kv.delete.assert_awaited_once_with(
            f"cache:{SYSTEM_DEFAULT_DOMAIN}:{ALIAS}"
        )

    @pytest.mark.asyncio
    async def test_og_links_go_through_the_writethrough(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        og_doc = make_url_v2_doc(meta_tags={"title": "T"})
        url_repo.iter_by_owner = _owner_iter([og_doc])
        url_repo.scrub_blocked_owner_pii = AsyncMock(return_value=0)
        url_repo.delete_by_owner = AsyncMock(return_value=1)
        og = MagicMock(remove=AsyncMock(), sync=AsyncMock())
        edge_kv = MagicMock()
        edge_kv.delete = AsyncMock(return_value=True)
        svc = make_service(
            url_repo,
            legacy_repo,
            emoji_repo,
            blocked_url_repo,
            url_cache,
            og_writethrough=og,
            edge_kv=edge_kv,
        )

        result = await svc.delete_all_by_owner(USER_OID)
        while svc._edge_purge_tasks:
            await asyncio.gather(*list(svc._edge_purge_tasks))

        assert result.deleted == 1
        og.remove.assert_awaited_once_with(SYSTEM_DEFAULT_DOMAIN, ALIAS)
        edge_kv.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_blocked_links_are_retained_scrubbed_and_still_counted(self):
        """The abuse audit trail survives erasure: BLOCKED docs are scrubbed
        in place, never deleted, and their ids still feed the click cascade."""
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        active = make_url_v2_doc()
        blocked = make_url_v2_doc(alias="badbad1", url_id=ObjectId(), status="BLOCKED")
        url_repo.iter_by_owner = _owner_iter([active, blocked])
        url_repo.scrub_blocked_owner_pii = AsyncMock(return_value=1)
        url_repo.delete_by_owner = AsyncMock(return_value=1)
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )

        result = await svc.delete_all_by_owner(USER_OID)

        assert result.deleted == 1
        assert result.blocked_retained == 1
        # The BLOCKED link's clicks still die — its id must be in the list.
        assert result.url_ids == [active.id, blocked.id]
        url_repo.scrub_blocked_owner_pii.assert_awaited_once_with(USER_OID)
        # The cached projection carries the pre-scrub password hash, so the
        # BLOCKED link's cache entry is purged like everyone else's.
        invalidated = [c.args for c in url_cache.invalidate.await_args_list]
        assert (blocked.alias, SYSTEM_DEFAULT_DOMAIN) in invalidated


# ─────────────────────────────────────────────────────────────────────────────
# TestUrlServiceScheduling — starts_at mirrors expiry with the sign flipped
# ─────────────────────────────────────────────────────────────────────────────


def _matches_scheduled_clause(doc: UrlV2Doc, now: datetime) -> bool:
    """Python evaluation of effective_status_clause(SCHEDULED, now) — the
    referee for the predicate pin, mirroring _derived_scheduled_arm minus
    the expiry arms."""
    if doc.status != "ACTIVE" or _matches_expired_clause(doc, now):
        return False
    starts = doc.starts_at
    if starts is None:
        return False
    aware = starts if starts.tzinfo else starts.replace(tzinfo=timezone.utc)
    return aware > now


class TestUrlServiceScheduling:
    PAST_TS = int(time_module.time()) - 3600
    FUTURE_TS = int(time_module.time()) + 3600
    FALLBACK = "https://example.org/soon"

    def _svc(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        blocked_url_repo.get_patterns.return_value = []
        url_repo.check_alias_exists.return_value = False
        url_repo.insert.return_value = URL_OID
        url_repo.update.return_value = True
        return svc, url_repo, url_cache

    # ── resolve ──────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_cache_hit_future_start_raises_not_yet_live(self):
        svc, url_repo, url_cache = self._svc()
        url_cache.get.return_value = make_active_cache(
            start_time=self.FUTURE_TS, pre_start_url=self.FALLBACK
        )

        with pytest.raises(NotYetLiveError) as exc:
            await svc.resolve(ALIAS)

        assert exc.value.fallback_url == self.FALLBACK
        assert exc.value.status_code == 404
        # No persisted flip — the clock decides, nothing to write.
        url_repo.update.assert_not_called()
        url_cache.invalidate.assert_not_called()

    @pytest.mark.asyncio
    async def test_cache_hit_past_start_resolves(self):
        svc, _url_repo, url_cache = self._svc()
        cached = make_active_cache(start_time=self.PAST_TS)
        url_cache.get.return_value = cached

        result, _schema = await svc.resolve(ALIAS)

        assert result is cached

    @pytest.mark.asyncio
    async def test_db_path_future_start_raises_after_recache(self):
        svc, url_repo, url_cache = self._svc()
        url_cache.get.return_value = None
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(
            microsecond=0
        )
        url_repo.find_by_alias.return_value = make_url_v2_doc(starts_at=future)

        with pytest.raises(NotYetLiveError) as exc:
            await svc.resolve(ALIAS)

        assert exc.value.fallback_url is None
        # The entry is cached with its start_time, so the next hit answers
        # from Redis and flips at the instant without a DB read.
        url_cache.set.assert_awaited_once()
        cached = url_cache.set.call_args[0][1]
        assert cached.start_time == int(future.timestamp())

    @pytest.mark.asyncio
    async def test_expired_beats_scheduled(self):
        """A past expiry with a future start is dead, not pending."""
        svc, url_repo, url_cache = self._svc()
        url_cache.get.return_value = make_active_cache(
            expiration_time=self.PAST_TS, start_time=self.FUTURE_TS
        )
        url_repo.expire_if_time_reached.return_value = True

        with pytest.raises(GoneError):
            await svc.resolve(ALIAS)

    @pytest.mark.asyncio
    async def test_blocked_beats_scheduled(self):
        svc, _url_repo, url_cache = self._svc()
        url_cache.get.return_value = make_active_cache(
            start_time=self.FUTURE_TS, url_status="BLOCKED"
        )

        with pytest.raises(BlockedUrlError):
            await svc.resolve(ALIAS)

    # ── create ───────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_create_future_start_is_stored(self):
        svc, url_repo, _url_cache = self._svc()
        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(
            long_url="https://example.com",
            starts_at=self.FUTURE_TS,
            pre_start_url=self.FALLBACK,
        )
        await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")

        inserted = url_repo.insert.call_args[0][0]
        assert inserted["starts_at"] == datetime.fromtimestamp(
            self.FUTURE_TS, tz=timezone.utc
        )
        assert inserted["pre_start_url"] == self.FALLBACK

    @pytest.mark.asyncio
    async def test_create_past_start_rejected(self):
        svc, *_ = self._svc()
        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(long_url="https://example.com", starts_at=self.PAST_TS)
        with pytest.raises(ValidationError, match="starts_at must be in the future"):
            await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")

    @pytest.mark.asyncio
    async def test_create_start_after_expiry_rejected(self):
        svc, *_ = self._svc()
        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(
            long_url="https://example.com",
            starts_at=self.FUTURE_TS + 60,
            expire_after=self.FUTURE_TS,
        )
        with pytest.raises(ValidationError, match="before expire_after"):
            await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")

    @pytest.mark.asyncio
    async def test_create_pre_start_url_runs_the_destination_gate(self):
        """A self-link fallback is refused like a self-link destination."""
        svc, *_ = self._svc()
        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(
            long_url="https://example.com",
            starts_at=self.FUTURE_TS,
            pre_start_url=f"https://{SYSTEM_DEFAULT_DOMAIN}/other",
        )
        with pytest.raises(ValidationError) as exc:
            await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")
        assert exc.value.field == "pre_start_url"

    # ── update ───────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_update_sets_and_clears_start(self):
        svc, url_repo, _url_cache = self._svc()
        from app.schemas.dto.requests.url import UpdateUrlRequest

        url_repo.find_by_id.return_value = make_url_v2_doc()
        await svc.update(URL_OID, UpdateUrlRequest(starts_at=self.FUTURE_TS), USER_OID)
        set_ops = url_repo.update.call_args[0][1]["$set"]
        assert set_ops["starts_at"] == datetime.fromtimestamp(
            self.FUTURE_TS, tz=timezone.utc
        )

        url_repo.find_by_id.return_value = make_url_v2_doc(
            starts_at=datetime.now(timezone.utc) + timedelta(days=1),
            pre_start_url=self.FALLBACK,
        )
        await svc.update(
            URL_OID, UpdateUrlRequest(starts_at=None, pre_start_url=None), USER_OID
        )
        set_ops = url_repo.update.call_args[0][1]["$set"]
        assert set_ops["starts_at"] is None
        assert set_ops["pre_start_url"] is None

    @pytest.mark.asyncio
    async def test_update_start_after_existing_expiry_rejected(self):
        svc, url_repo, _url_cache = self._svc()
        from app.schemas.dto.requests.url import UpdateUrlRequest

        url_repo.find_by_id.return_value = make_url_v2_doc(
            expire_after=datetime.fromtimestamp(self.FUTURE_TS, tz=timezone.utc)
        )
        with pytest.raises(ValidationError, match="before expire_after"):
            await svc.update(
                URL_OID, UpdateUrlRequest(starts_at=self.FUTURE_TS + 60), USER_OID
            )

    @pytest.mark.asyncio
    async def test_update_start_against_naive_stored_expiry_is_a_400_not_a_500(self):
        """Mongo hands back naive UTC; the request side is aware. The order
        check must normalise both or the comparison raises TypeError."""
        svc, url_repo, _url_cache = self._svc()
        from app.schemas.dto.requests.url import UpdateUrlRequest

        naive_expiry = datetime.fromtimestamp(self.FUTURE_TS, tz=timezone.utc).replace(
            tzinfo=None
        )
        url_repo.find_by_id.return_value = make_url_v2_doc(expire_after=naive_expiry)
        with pytest.raises(ValidationError, match="before expire_after"):
            await svc.update(
                URL_OID, UpdateUrlRequest(starts_at=self.FUTURE_TS + 60), USER_OID
            )

    @pytest.mark.asyncio
    async def test_update_expiry_against_naive_stored_start_is_a_400_not_a_500(self):
        svc, url_repo, _url_cache = self._svc()
        from app.schemas.dto.requests.url import UpdateUrlRequest

        naive_start = datetime.fromtimestamp(self.FUTURE_TS, tz=timezone.utc).replace(
            tzinfo=None
        )
        url_repo.find_by_id.return_value = make_url_v2_doc(starts_at=naive_start)
        with pytest.raises(ValidationError, match="before expire_after"):
            await svc.update(
                URL_OID, UpdateUrlRequest(expire_after=self.FUTURE_TS - 60), USER_OID
            )

    @pytest.mark.asyncio
    async def test_update_unchanged_start_is_a_noop(self):
        svc, url_repo, _url_cache = self._svc()
        from app.schemas.dto.requests.url import UpdateUrlRequest

        starts = datetime.fromtimestamp(self.FUTURE_TS, tz=timezone.utc)
        url_repo.find_by_id.return_value = make_url_v2_doc(starts_at=starts)
        await svc.update(URL_OID, UpdateUrlRequest(starts_at=self.FUTURE_TS), USER_OID)
        url_repo.update.assert_not_called()

    # ── list filter ──────────────────────────────────────────────────────

    def test_scheduled_clause_shape(self):
        from app.services.url_service import (
            _derived_expiry_arms,
            effective_status_clause,
        )

        now = datetime.now(timezone.utc)
        assert effective_status_clause(UrlStatus.SCHEDULED, now) == {
            "status": UrlStatus.ACTIVE,
            "starts_at": {"$gt": now},
            "$nor": _derived_expiry_arms(now),
        }

    def test_predicate_pin_matrix(self):
        """effective_status == SCHEDULED iff doc matches the SCHEDULED
        clause, across status x start x expiry x max-clicks."""
        now = datetime.now(timezone.utc)
        statuses = ["ACTIVE", "INACTIVE", "EXPIRED", "BLOCKED"]
        instants = [None, now - timedelta(hours=1), now + timedelta(hours=1)]
        clicks = [(None, 0), (5, 5)]

        for status in statuses:
            for starts_at in instants:
                for expire_after in instants:
                    for max_clicks, total_clicks in clicks:
                        doc = make_url_v2_doc(
                            status=status,
                            starts_at=starts_at,
                            expire_after=expire_after,
                            max_clicks=max_clicks,
                        )
                        doc.total_clicks = total_clicks
                        case = f"{status}/{starts_at}/{expire_after}/{max_clicks}"
                        assert (doc.effective_status == UrlStatus.SCHEDULED) == (
                            _matches_scheduled_clause(doc, now)
                        ), case


class TestSecondaryDestinationStamping:
    """A blocked host hidden in a geo rule must be findable by host."""

    @pytest.mark.asyncio
    async def test_create_with_geo_rules_stamps_secondary_hosts(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        blocked_url_repo.get_patterns.return_value = []
        url_repo.check_alias_exists.return_value = False
        url_repo.insert.return_value = URL_OID

        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(
            long_url="https://clean.example/",
            geo_rules={"IN": "https://hidden.example/kit"},
        )
        await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")

        inserted = url_repo.insert.call_args.args[0]
        assert inserted["dest"]["host"] == "clean.example"
        assert inserted["dest"]["secondary_hosts"] == ["hidden.example"]

    @pytest.mark.asyncio
    async def test_create_without_geo_rules_writes_no_secondary_field(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        blocked_url_repo.get_patterns.return_value = []
        url_repo.check_alias_exists.return_value = False
        url_repo.insert.return_value = URL_OID

        from app.schemas.dto.requests.url import CreateUrlRequest

        await svc.create(
            CreateUrlRequest(long_url="https://clean.example/"),
            owner_id=USER_OID,
            client_ip="1.2.3.4",
        )
        # Absent, not [] or null: the sparse index holds only real cases.
        assert "secondary_hosts" not in url_repo.insert.call_args.args[0]["dest"]

    @pytest.mark.asyncio
    async def test_update_geo_rules_restamps_secondary_hosts(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        blocked_url_repo.get_patterns.return_value = []
        existing = make_url_v2_doc()
        url_repo.find_by_id.return_value = existing
        url_repo.update.return_value = True

        from app.schemas.dto.requests.url import UpdateUrlRequest

        req = UpdateUrlRequest(geo_rules={"BR": "https://hidden.example/br"})
        await svc.update(URL_OID, req, USER_OID)

        written = url_repo.update.call_args[0][1]["$set"]
        assert written["geo_rules"] == {"BR": "https://hidden.example/br"}
        assert written["dest"]["secondary_hosts"] == ["hidden.example"]
        from app.shared.url_utils import parse_destination

        assert written["dest"]["host"] == parse_destination(existing.long_url)["host"]

    @pytest.mark.asyncio
    async def test_create_with_ab_variants_stamps_secondary_hosts(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        blocked_url_repo.get_patterns.return_value = []
        url_repo.check_alias_exists.return_value = False
        url_repo.insert.return_value = URL_OID

        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(
            long_url="https://clean.example/",
            geo_rules={"IN": "https://geo.example/in"},
            ab_variants=[
                {"url": "https://hidden.example/b", "weight": 30},
                {"url": "https://clean.example/c", "weight": 30},
            ],
        )
        await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")

        inserted = url_repo.insert.call_args.args[0]
        assert inserted["dest"]["secondary_hosts"] == ["geo.example", "hidden.example"]

    @pytest.mark.asyncio
    async def test_update_ab_variants_restamps_secondary_hosts(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        existing = make_url_v2_doc()
        url_repo.find_by_id.return_value = existing
        url_repo.update.return_value = existing
        blocked_url_repo.get_patterns.return_value = []

        from app.schemas.dto.requests.url import UpdateUrlRequest

        req = UpdateUrlRequest(
            ab_variants=[{"url": "https://hidden.example/b", "weight": 40}]
        )
        await svc.update(URL_OID, req, USER_OID)

        written = url_repo.update.call_args[0][1]["$set"]
        assert written["dest"]["secondary_hosts"] == ["hidden.example"]

    @pytest.mark.asyncio
    async def test_clearing_ab_variants_drops_secondary_hosts(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        existing = make_url_v2_doc(
            ab_variants=[{"url": "https://hidden.example/b", "weight": 40}]
        )
        url_repo.find_by_id.return_value = existing
        url_repo.update.return_value = make_url_v2_doc()

        from app.schemas.dto.requests.url import UpdateUrlRequest

        await svc.update(URL_OID, UpdateUrlRequest(ab_variants=None), USER_OID)

        written = url_repo.update.call_args[0][1]["$set"]
        assert written["ab_variants"] is None
        assert "secondary_hosts" not in written["dest"]

    @pytest.mark.asyncio
    async def test_clearing_geo_rules_drops_secondary_hosts(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        existing = make_url_v2_doc()
        existing.geo_rules = {"BR": "https://hidden.example/br"}
        url_repo.find_by_id.return_value = existing
        url_repo.update.return_value = True

        from app.schemas.dto.requests.url import UpdateUrlRequest

        await svc.update(URL_OID, UpdateUrlRequest(geo_rules=None), USER_OID)

        written = url_repo.update.call_args[0][1]["$set"]
        assert written["geo_rules"] is None
        assert "secondary_hosts" not in written["dest"]


class TestPreStartUrlStamping:
    @pytest.mark.asyncio
    async def test_create_with_pre_start_url_stamps_its_host(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        blocked_url_repo.get_patterns.return_value = []
        url_repo.check_alias_exists.return_value = False
        url_repo.insert.return_value = URL_OID

        from datetime import timedelta

        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(
            long_url="https://clean.example/",
            starts_at=datetime.now(timezone.utc) + timedelta(days=1),
            pre_start_url="https://teaser.example/soon",
        )
        await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")

        inserted = url_repo.insert.call_args.args[0]
        assert inserted["dest"]["secondary_hosts"] == ["teaser.example"]

    @pytest.mark.asyncio
    async def test_update_pre_start_url_restamps_dest(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        blocked_url_repo.get_patterns.return_value = []
        existing = make_url_v2_doc()
        url_repo.find_by_id.return_value = existing
        url_repo.update.return_value = True

        from app.schemas.dto.requests.url import UpdateUrlRequest

        await svc.update(
            URL_OID,
            UpdateUrlRequest(pre_start_url="https://teaser.example/soon"),
            USER_OID,
        )

        written = url_repo.update.call_args[0][1]["$set"]
        assert written["pre_start_url"] == "https://teaser.example/soon"
        assert written["dest"]["secondary_hosts"] == ["teaser.example"]


class TestL1SeesSecondaryDestinations:
    @pytest.mark.asyncio
    async def test_create_records_every_destination_for_burst_counters(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        blocked_url_repo.get_patterns.return_value = []
        url_repo.check_alias_exists.return_value = False
        url_repo.insert.return_value = URL_OID
        svc._url_policy.record_create = AsyncMock()

        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(
            long_url="https://clean.example/",
            geo_rules={
                "IN": "https://hidden.example/kit",
                "US": "https://clean.example/",
            },
            starts_at=datetime.now(timezone.utc) + timedelta(days=1),
            pre_start_url="https://teaser.example/soon",
        )
        await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")

        recorded = [c.args[0] for c in svc._url_policy.record_create.await_args_list]
        # Distinct URLs, main first: the geo rule echoing long_url counts once.
        assert recorded == [
            "https://clean.example/",
            "https://hidden.example/kit",
            "https://teaser.example/soon",
        ]

    @pytest.mark.asyncio
    async def test_many_geo_paths_on_one_domain_count_once(self):
        url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
        svc = make_service(
            url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache
        )
        blocked_url_repo.get_patterns.return_value = []
        url_repo.check_alias_exists.return_value = False
        url_repo.insert.return_value = URL_OID
        svc._url_policy.record_create = AsyncMock()

        from app.schemas.dto.requests.url import CreateUrlRequest

        codes = ["IN", "US", "DE", "FR", "BR", "JP", "GB", "CA", "AU", "ES"]
        req = CreateUrlRequest(
            long_url="https://x.example/",
            geo_rules={c: f"https://x.example/{c.lower()}" for c in codes},
        )
        await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")

        # Counters key on the registrable domain: one create is one bump.
        assert svc._url_policy.record_create.await_count == 1
