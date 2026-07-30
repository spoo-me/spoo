"""
Unit tests for Phase 7 — ClickService, V2ClickHandler, LegacyClickHandler.

All external dependencies (repositories, GeoIP, cache) are replaced with AsyncMock.
Tests verify:
- V2ClickHandler inserts ClickDoc and increments URL click count
- Blocked bots for v2 skip analytics but do NOT raise (redirect still happens)
- Blocked bots for v1/emoji raise ForbiddenError (redirect is also blocked)
- Max-clicks expiry invalidates the URL cache
- GeoIP failure falls back to "Unknown"
- Invalid/missing User-Agent raises ValidationError
- LegacyClickHandler builds the correct $inc/$set/$addToSet update document
- ClickService dispatches to the correct handler based on schema
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId
from ua_parser import parse as ua_parse

from errors import ForbiddenError, ValidationError
from infrastructure.cache.url_cache import UrlCacheData
from schemas.models.base import ANONYMOUS_OWNER_ID
from services.click import ClickService, LegacyClickHandler, V2ClickHandler
from services.click.handlers import classify_device
from services.click.protocol import ClickContext

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

USER_OID = ObjectId("aaaaaaaaaaaaaaaaaaaaaaaa")
URL_OID = ObjectId("bbbbbbbbbbbbbbbbbbbbbbbb")
ALIAS = "abc1234"
CLIENT_IP = "1.2.3.4"
REDIRECT_MS = 42
NORMAL_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0"
)
# Mobile Googlebot UA — has both user_agent AND os in ua_parser (bare Googlebot has os=None).
BOT_UA = (
    "Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/W.X.Y.Z Mobile Safari/537.36 "
    "(compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


DOMAIN = "spoo.me"


def make_v2_cache(
    block_bots: bool = False,
    max_clicks: int | None = None,
    owner_id: str | None = None,
    domain: str = DOMAIN,
) -> UrlCacheData:
    return UrlCacheData(
        id=str(URL_OID),
        alias=ALIAS,
        long_url="https://example.com",
        block_bots=block_bots,
        password_hash=None,
        expiration_time=None,
        max_clicks=max_clicks,
        url_status="ACTIVE",
        schema_version="v2",
        owner_id=owner_id or str(USER_OID),
        domain=domain,
    )


def make_v1_cache(
    block_bots: bool = False,
    short_code: str = "abcdef",
) -> UrlCacheData:
    return UrlCacheData(
        id=short_code,
        alias=short_code,
        long_url="https://legacy.example.com",
        block_bots=block_bots,
        password_hash=None,
        expiration_time=None,
        max_clicks=None,
        url_status="ACTIVE",
        schema_version="v1",
        owner_id=None,
    )


@dataclass
class TestDeps:
    click_repo: AsyncMock
    url_repo: AsyncMock
    legacy_repo: AsyncMock
    emoji_repo: AsyncMock
    geoip: AsyncMock
    url_cache: AsyncMock


def make_deps() -> TestDeps:
    deps = TestDeps(
        click_repo=AsyncMock(),
        url_repo=AsyncMock(),
        legacy_repo=AsyncMock(),
        emoji_repo=AsyncMock(),
        geoip=AsyncMock(),
        url_cache=AsyncMock(),
    )

    # sensible defaults
    deps.geoip.get_country.return_value = "United States"
    deps.geoip.get_city.return_value = "New York"
    deps.url_repo.increment_clicks.return_value = None
    deps.url_repo.expire_if_max_clicks.return_value = False
    deps.click_repo.insert.return_value = None

    return deps


def make_v2_handler(click_repo, url_repo, geoip, url_cache) -> V2ClickHandler:
    return V2ClickHandler(
        click_repo=click_repo,
        url_repo=url_repo,
        geoip=geoip,
        url_cache=url_cache,
    )


def make_legacy_handler(legacy_repo, emoji_repo, geoip) -> LegacyClickHandler:
    return LegacyClickHandler(
        legacy_repo=legacy_repo,
        emoji_repo=emoji_repo,
        geoip=geoip,
    )


def make_context(
    url_data: UrlCacheData,
    short_code: str = ALIAS,
    client_ip: str = CLIENT_IP,
    redirect_ms: int = REDIRECT_MS,
    user_agent: str = NORMAL_UA,
    referrer: str | None = None,
    is_emoji: bool = False,
    cf_city: str | None = None,
    utm_source: str | None = None,
    utm_medium: str | None = None,
    utm_campaign: str | None = None,
) -> ClickContext:
    return ClickContext(
        url_data=url_data,
        short_code=short_code,
        client_ip=client_ip,
        redirect_ms=redirect_ms,
        user_agent=user_agent,
        referrer=referrer,
        is_emoji=is_emoji,
        cf_city=cf_city,
        utm_source=utm_source,
        utm_medium=utm_medium,
        utm_campaign=utm_campaign,
    )


# ─────────────────────────────────────────────────────────────────────────────
# TestClassifyDevice
# ─────────────────────────────────────────────────────────────────────────────


class TestClassifyDevice:
    @pytest.mark.parametrize(
        ("user_agent", "expected"),
        [
            # iPhone Safari
            (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
                "Mobile/15E148 Safari/604.1",
                "mobile",
            ),
            # iPad with the pre-iPadOS-13 (non-desktop-mode) UA
            (
                "Mozilla/5.0 (iPad; CPU OS 12_2 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/12.1 "
                "Mobile/15E148 Safari/604.1",
                "tablet",
            ),
            # Chrome on Android phone, post-UA-reduction (model frozen to "K")
            (
                "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36",
                "mobile",
            ),
            # Chrome on Android tablet — no "Mobile" token
            (
                "Mozilla/5.0 (Linux; Android 13; SM-X906C) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                "tablet",
            ),
            # Windows desktop
            (NORMAL_UA, "desktop"),
            # macOS desktop — also where iPad Safari lands since iPadOS 13
            # (desktop-mode UA is byte-identical to a real Mac; accepted)
            (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
                "Safari/605.1.15",
                "desktop",
            ),
            # Linux desktop
            (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                "desktop",
            ),
        ],
    )
    def test_buckets_representative_uas(self, user_agent: str, expected: str):
        assert classify_device(ua_parse(user_agent), user_agent) == expected

    def test_unrecognized_ua_is_unknown(self):
        ua = "SomeExoticClient/1.0"
        assert classify_device(ua_parse(ua), ua) == "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# TestV2ClickHandler
# ─────────────────────────────────────────────────────────────────────────────


class TestV2ClickHandler:
    @pytest.mark.asyncio
    async def test_inserts_click_and_increments_url(self):
        d = make_deps()
        handler = make_v2_handler(d.click_repo, d.url_repo, d.geoip, d.url_cache)
        url_data = make_v2_cache()

        await handler.handle(make_context(url_data))

        d.click_repo.insert.assert_called_once()
        d.url_repo.increment_clicks.assert_called_once()

    @pytest.mark.asyncio
    async def test_click_doc_has_correct_fields(self):
        d = make_deps()
        handler = make_v2_handler(d.click_repo, d.url_repo, d.geoip, d.url_cache)
        url_data = make_v2_cache()

        await handler.handle(make_context(url_data))

        doc = d.click_repo.insert.call_args[0][0]
        assert "clicked_at" in doc
        assert "meta" in doc
        assert doc["meta"]["short_code"] == ALIAS
        assert doc["ip_address"] == CLIENT_IP
        assert doc["country"] == "United States"

    @pytest.mark.asyncio
    async def test_device_recorded_in_click_doc(self):
        d = make_deps()
        handler = make_v2_handler(d.click_repo, d.url_repo, d.geoip, d.url_cache)
        url_data = make_v2_cache()

        await handler.handle(make_context(url_data))  # NORMAL_UA is Windows

        doc = d.click_repo.insert.call_args[0][0]
        assert doc["device"] == "desktop"

    @pytest.mark.asyncio
    async def test_utm_tags_recorded_in_click_doc(self):
        d = make_deps()
        handler = make_v2_handler(d.click_repo, d.url_repo, d.geoip, d.url_cache)
        url_data = make_v2_cache()

        await handler.handle(
            make_context(
                url_data,
                utm_source="newsletter",
                utm_medium="email",
                utm_campaign="launch",
            )
        )

        doc = d.click_repo.insert.call_args[0][0]
        assert doc["utm_source"] == "newsletter"
        assert doc["utm_medium"] == "email"
        assert doc["utm_campaign"] == "launch"

    @pytest.mark.asyncio
    async def test_utm_tags_default_to_none(self):
        d = make_deps()
        handler = make_v2_handler(d.click_repo, d.url_repo, d.geoip, d.url_cache)
        url_data = make_v2_cache()

        await handler.handle(make_context(url_data))

        # Absent, not null: explicit nulls close time-series buckets.
        doc = d.click_repo.insert.call_args[0][0]
        assert "utm_source" not in doc
        assert "utm_medium" not in doc
        assert "utm_campaign" not in doc

    @pytest.mark.asyncio
    async def test_meta_carries_domain_from_cache(self):
        d = make_deps()
        handler = make_v2_handler(d.click_repo, d.url_repo, d.geoip, d.url_cache)
        url_data = make_v2_cache(domain="links.acme.com")

        await handler.handle(make_context(url_data))

        doc = d.click_repo.insert.call_args[0][0]
        assert doc["meta"]["domain"] == "links.acme.com"

    @pytest.mark.asyncio
    async def test_meta_domain_omitted_when_cache_empty_string(self):
        # Older cached entries pre-PR1 have domain="". Coerced to None and
        # then omitted from the insert doc ($eq: null matches missing, so
        # per-domain queries still distinguish "unknown" from a real value).
        d = make_deps()
        handler = make_v2_handler(d.click_repo, d.url_repo, d.geoip, d.url_cache)
        url_data = make_v2_cache(domain="")

        await handler.handle(make_context(url_data))

        doc = d.click_repo.insert.call_args[0][0]
        assert "domain" not in doc["meta"]

    @pytest.mark.asyncio
    async def test_blocked_bot_skips_analytics_no_error(self):
        """v2: blocked bot → skip analytics, no exception raised (redirect proceeds)."""
        d = make_deps()
        handler = make_v2_handler(d.click_repo, d.url_repo, d.geoip, d.url_cache)
        url_data = make_v2_cache(block_bots=True)

        with patch("services.click.handlers.is_bot_request", return_value=True):
            # Should NOT raise
            await handler.handle(make_context(url_data, user_agent=BOT_UA))

        d.click_repo.insert.assert_not_called()
        d.url_repo.increment_clicks.assert_not_called()

    @pytest.mark.asyncio
    async def test_unblocked_bot_still_tracked(self):
        """v2 with block_bots=False: bot is still tracked normally."""
        d = make_deps()
        handler = make_v2_handler(d.click_repo, d.url_repo, d.geoip, d.url_cache)
        url_data = make_v2_cache(block_bots=False)

        with (
            patch("services.click.handlers.is_bot_request", return_value=True),
            patch("services.click.handlers.get_bot_name", return_value="Googlebot"),
        ):
            await handler.handle(make_context(url_data, user_agent=BOT_UA))

        d.click_repo.insert.assert_called_once()
        d.url_repo.increment_clicks.assert_called_once()

    @pytest.mark.asyncio
    async def test_max_clicks_expiry_invalidates_cache(self):
        d = make_deps()
        handler = make_v2_handler(d.click_repo, d.url_repo, d.geoip, d.url_cache)
        url_data = make_v2_cache(max_clicks=100)
        d.url_repo.expire_if_max_clicks.return_value = True  # URL just expired

        await handler.handle(make_context(url_data))

        d.url_repo.expire_if_max_clicks.assert_called_once_with(URL_OID, 100)
        d.url_cache.invalidate.assert_called_once_with(ALIAS, DOMAIN)

    @pytest.mark.asyncio
    async def test_max_clicks_not_reached_no_cache_invalidation(self):
        d = make_deps()
        handler = make_v2_handler(d.click_repo, d.url_repo, d.geoip, d.url_cache)
        url_data = make_v2_cache(max_clicks=100)
        d.url_repo.expire_if_max_clicks.return_value = False

        await handler.handle(make_context(url_data))

        d.url_cache.invalidate.assert_not_called()

    @pytest.mark.asyncio
    async def test_geoip_failure_falls_back_to_unknown(self):
        d = make_deps()
        d.geoip.get_country.return_value = "Unknown"
        d.geoip.get_city.return_value = None
        handler = make_v2_handler(d.click_repo, d.url_repo, d.geoip, d.url_cache)
        url_data = make_v2_cache()

        await handler.handle(make_context(url_data))

        doc = d.click_repo.insert.call_args[0][0]
        assert doc["country"] == "Unknown"
        assert doc["city"] == "Unknown"

    @pytest.mark.asyncio
    async def test_cf_city_fallback_when_geoip_returns_none(self):
        d = make_deps()
        d.geoip.get_city.return_value = None  # GeoIP city unavailable
        handler = make_v2_handler(d.click_repo, d.url_repo, d.geoip, d.url_cache)
        url_data = make_v2_cache()

        await handler.handle(make_context(url_data, cf_city="London"))

        doc = d.click_repo.insert.call_args[0][0]
        assert doc["city"] == "London"

    @pytest.mark.asyncio
    async def test_empty_user_agent_raises_validation_error(self):
        d = make_deps()
        handler = make_v2_handler(d.click_repo, d.url_repo, d.geoip, d.url_cache)
        url_data = make_v2_cache()

        with pytest.raises(ValidationError):
            await handler.handle(make_context(url_data, user_agent=""))

    @pytest.mark.asyncio
    async def test_referrer_is_sanitized_in_click_doc(self):
        d = make_deps()
        handler = make_v2_handler(d.click_repo, d.url_repo, d.geoip, d.url_cache)
        url_data = make_v2_cache()

        await handler.handle(
            make_context(
                url_data,
                referrer="https://www.google.com/search?q=test",
            )
        )

        doc = d.click_repo.insert.call_args[0][0]
        # referrer should be domain only, sanitized
        assert doc["referrer"] == "google.com"

    @pytest.mark.asyncio
    async def test_anonymous_owner_uses_sentinel(self):
        d = make_deps()
        handler = make_v2_handler(d.click_repo, d.url_repo, d.geoip, d.url_cache)
        url_data = make_v2_cache(owner_id=None)
        url_data.owner_id = None  # override

        await handler.handle(make_context(url_data))

        doc = d.click_repo.insert.call_args[0][0]
        assert doc["meta"]["owner_id"] == ANONYMOUS_OWNER_ID


# ─────────────────────────────────────────────────────────────────────────────
# TestLegacyClickHandler
# ─────────────────────────────────────────────────────────────────────────────


class TestLegacyClickHandler:
    @pytest.mark.asyncio
    async def test_builds_update_doc_and_calls_legacy_repo(self):
        d = make_deps()
        handler = make_legacy_handler(d.legacy_repo, d.emoji_repo, d.geoip)
        url_data = make_v1_cache(short_code="abcdef")

        with patch("services.click.handlers.is_bot_request", return_value=False):
            await handler.handle(
                make_context(url_data, short_code="abcdef", is_emoji=False)
            )

        d.legacy_repo.update.assert_called_once()
        update_doc = d.legacy_repo.update.call_args[0][1]
        assert "$inc" in update_doc
        assert "$set" in update_doc
        assert "$addToSet" in update_doc
        assert update_doc["$inc"]["total-clicks"] == 1

    @pytest.mark.asyncio
    async def test_emoji_url_calls_emoji_repo(self):
        d = make_deps()
        handler = make_legacy_handler(d.legacy_repo, d.emoji_repo, d.geoip)
        url_data = make_v1_cache(short_code="🐍🔥💎")
        url_data.schema_version = "emoji"

        with patch("services.click.handlers.is_bot_request", return_value=False):
            await handler.handle(
                make_context(url_data, short_code="🐍🔥💎", is_emoji=True)
            )

        d.emoji_repo.update.assert_called_once()
        d.legacy_repo.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_blocked_bot_v1_raises_forbidden(self):
        """v1: blocked bot → ForbiddenError (redirect is also blocked)."""
        d = make_deps()
        handler = make_legacy_handler(d.legacy_repo, d.emoji_repo, d.geoip)
        url_data = make_v1_cache(block_bots=True, short_code="abcdef")

        with (
            patch("services.click.handlers.is_bot_request", return_value=True),
            pytest.raises(ForbiddenError),
        ):
            await handler.handle(
                make_context(
                    url_data,
                    short_code="abcdef",
                    user_agent=BOT_UA,
                    is_emoji=False,
                )
            )

        d.legacy_repo.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_unblocked_bot_v1_tracked_no_error(self):
        """v1 with block_bots=False: bot is counted but no exception."""
        d = make_deps()
        handler = make_legacy_handler(d.legacy_repo, d.emoji_repo, d.geoip)
        url_data = make_v1_cache(block_bots=False, short_code="abcdef")

        with (
            patch("services.click.handlers.is_bot_request", return_value=True),
            patch("services.click.handlers.get_bot_name", return_value="Googlebot"),
        ):
            await handler.handle(
                make_context(
                    url_data,
                    short_code="abcdef",
                    user_agent=BOT_UA,
                    is_emoji=False,
                )
            )

        d.legacy_repo.update.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_user_agent_raises_validation_error(self):
        d = make_deps()
        handler = make_legacy_handler(d.legacy_repo, d.emoji_repo, d.geoip)
        url_data = make_v1_cache()

        with pytest.raises(ValidationError):
            await handler.handle(make_context(url_data, user_agent=""))

    @pytest.mark.asyncio
    async def test_referrer_added_to_update_doc(self):
        d = make_deps()
        handler = make_legacy_handler(d.legacy_repo, d.emoji_repo, d.geoip)
        url_data = make_v1_cache(short_code="abcdef")

        with patch("services.click.handlers.is_bot_request", return_value=False):
            await handler.handle(
                make_context(
                    url_data,
                    short_code="abcdef",
                    referrer="https://www.twitter.com/home",
                    is_emoji=False,
                )
            )

        update_doc = d.legacy_repo.update.call_args[0][1]
        inc_keys = list(update_doc["$inc"].keys())
        referrer_keys = [k for k in inc_keys if k.startswith("referrer.")]
        assert len(referrer_keys) > 0

    @pytest.mark.asyncio
    async def test_last_click_metadata_in_set(self):
        d = make_deps()
        handler = make_legacy_handler(d.legacy_repo, d.emoji_repo, d.geoip)
        url_data = make_v1_cache(short_code="abcdef")

        with patch("services.click.handlers.is_bot_request", return_value=False):
            await handler.handle(
                make_context(url_data, short_code="abcdef", is_emoji=False)
            )

        update_doc = d.legacy_repo.update.call_args[0][1]
        assert "last-click" in update_doc["$set"]
        assert "last-click-browser" in update_doc["$set"]
        assert "last-click-os" in update_doc["$set"]
        assert "average_redirection_time" in update_doc["$set"]

    @pytest.mark.asyncio
    async def test_unique_click_counter_incremented(self):
        """Every click is treated as unique since ips are not tracked in UrlCacheData."""
        d = make_deps()
        handler = make_legacy_handler(d.legacy_repo, d.emoji_repo, d.geoip)
        url_data = make_v1_cache(short_code="abcdef")

        with patch("services.click.handlers.is_bot_request", return_value=False):
            await handler.handle(
                make_context(url_data, short_code="abcdef", is_emoji=False)
            )

        update_doc = d.legacy_repo.update.call_args[0][1]
        unique_keys = [k for k in update_doc["$inc"] if k.startswith("unique_counter.")]
        assert len(unique_keys) == 1


# ─────────────────────────────────────────────────────────────────────────────
# TestTrackClickDispatch
# ─────────────────────────────────────────────────────────────────────────────


class TestTrackClickDispatch:
    @pytest.mark.asyncio
    async def test_dispatches_v2_schema_to_v2_handler(self):
        v2_handler = AsyncMock()
        legacy_handler = AsyncMock()
        svc = ClickService(handlers={"v2": v2_handler, "v1": legacy_handler})
        url_data = make_v2_cache()

        await svc.track_click(
            url_data,
            ALIAS,
            schema="v2",
            is_emoji=False,
            client_ip=CLIENT_IP,
            redirect_ms=REDIRECT_MS,
            user_agent=NORMAL_UA,
            referrer=None,
        )

        v2_handler.handle.assert_called_once()
        legacy_handler.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatches_v1_schema_to_legacy_handler(self):
        v2_handler = AsyncMock()
        legacy_handler = AsyncMock()
        svc = ClickService(handlers={"v2": v2_handler, "v1": legacy_handler})
        url_data = make_v1_cache()

        await svc.track_click(
            url_data,
            "abcdef",
            schema="v1",
            is_emoji=False,
            client_ip=CLIENT_IP,
            redirect_ms=REDIRECT_MS,
            user_agent=NORMAL_UA,
            referrer=None,
        )

        legacy_handler.handle.assert_called_once()
        v2_handler.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatches_emoji_schema_to_legacy_handler_with_is_emoji_true(self):
        v2_handler = AsyncMock()
        legacy_handler = AsyncMock()
        svc = ClickService(handlers={"v2": v2_handler, "v1": legacy_handler})
        url_data = make_v1_cache()
        url_data.schema_version = "emoji"

        await svc.track_click(
            url_data,
            "🐍🔥💎",
            schema="emoji",
            is_emoji=True,
            client_ip=CLIENT_IP,
            redirect_ms=REDIRECT_MS,
            user_agent=NORMAL_UA,
            referrer=None,
        )

        # "emoji" not in handlers → falls back to "v1" (legacy handler)
        legacy_handler.handle.assert_called_once()
        v2_handler.handle.assert_not_called()

        # Verify is_emoji=True was passed through via ClickContext
        context: ClickContext = legacy_handler.handle.call_args[0][0]
        assert context.is_emoji is True
