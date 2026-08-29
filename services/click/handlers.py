"""
Click handlers — one class per URL schema.

Each handler implements the ClickHandler protocol, receives its dependencies
via constructor injection, and reads all click metadata from the ClickContext.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from bson import ObjectId
from ua_parser import Result
from ua_parser import parse as ua_parse

from errors import ForbiddenError, ValidationError
from infrastructure.cache.url_cache import UrlCache
from infrastructure.geoip import GeoIPService
from infrastructure.logging import get_logger, should_sample
from repositories.click_repository import ClickRepository
from repositories.legacy.emoji_url_repository import EmojiUrlRepository
from repositories.legacy.legacy_url_repository import LegacyUrlRepository
from repositories.url_repository import UrlRepository
from schemas.models.base import ANONYMOUS_OWNER_ID
from schemas.models.click import ClickDoc, ClickMeta
from services.click.bot_detection import get_bot_name, is_bot_request
from services.click.protocol import ClickContext
from services.events.protocol import DomainEventSink
from shared.url_utils import registrable_domain

log = get_logger(__name__)

_DESKTOP_OS_FAMILIES = frozenset(
    {"Windows", "Mac OS X", "Linux", "Chrome OS", "Ubuntu", "Fedora"}
)
_MOBILE_OS_FAMILIES = frozenset({"Windows Phone", "KaiOS", "Firefox OS"})


def classify_device(ua: Result, user_agent: str) -> str:
    """Bucket a parsed UA into ``mobile`` / ``tablet`` / ``desktop`` / ``unknown``.

    ua-parser reports device family/brand/model but deliberately no type;
    this is the token fallback chain Matomo's DeviceDetector itself uses
    when its model regexes don't decide. The signals survive Chrome's UA
    reduction (the "Mobile" token and OS family are preserved; the device
    model is frozen to "K"). Known, accepted limit: iPad Safari sends a
    Mac UA since iPadOS 13 and lands in ``desktop`` — indistinguishable
    server-side, counted the same way by GA4/Adobe/Matomo.
    """
    os_family = ua.os.family if ua.os else ""
    device_family = ua.device.family if ua.device else ""
    if os_family == "iOS":
        return "tablet" if "iPad" in device_family else "mobile"
    if os_family == "Android":
        # Chrome on Android carries "Mobile" on phones only; tablets omit it
        return "mobile" if "Mobile" in user_agent else "tablet"
    if os_family in _DESKTOP_OS_FAMILIES:
        return "desktop"
    if os_family in _MOBILE_OS_FAMILIES:
        return "mobile"
    if device_family == "Generic Smartphone":
        return "mobile"
    if device_family == "Generic Tablet":
        return "tablet"
    return "unknown"


class V2ClickHandler:
    """Records a click for a v2 URL in the time-series clicks collection."""

    def __init__(
        self,
        click_repo: ClickRepository,
        url_repo: UrlRepository,
        geoip: GeoIPService,
        url_cache: UrlCache,
        events: DomainEventSink | None = None,
    ) -> None:
        self._click_repo = click_repo
        self._url_repo = url_repo
        self._geoip = geoip
        self._url_cache = url_cache
        # Domain-event sink (webhooks backbone); None = feature off.
        self._events = events

    async def handle(self, context: ClickContext) -> None:
        """
        Record a click for a v2 URL in the time-series clicks collection.

        If block_bots is True and the request is from a bot, analytics are
        skipped but no exception is raised — the redirect still proceeds.

        Raises:
            ValidationError: Missing or unparseable User-Agent header.
        """
        url_data = context.url_data
        short_code = context.short_code
        client_ip = context.client_ip
        user_agent = context.user_agent
        referrer = context.referrer
        cf_city = context.cf_city

        if not user_agent:
            raise ValidationError("Invalid User-Agent")

        try:
            ua = ua_parse(user_agent)
        except Exception:
            log.warning(
                "ua_parse_failed",
                schema="v2",
                user_agent=user_agent[:200],
            )
            raise ValidationError(
                "An internal error occurred while processing the User-Agent"
            ) from None

        if not ua or not ua.user_agent or not ua.os:
            log.debug("ua_invalid", schema="v2", user_agent=user_agent[:200])
            raise ValidationError("Invalid User-Agent")

        os_name = ua.os.family
        browser = ua.user_agent.family
        device = classify_device(ua, user_agent)

        # Referrer sanitization (v2 style)
        sanitized_referrer: str | None = None
        if referrer:
            domain = registrable_domain(referrer)
            # Remove MongoDB-unsafe chars and control characters
            domain = re.sub(r"[$\x00-\x1F\x7F-\x9F]", "_", domain)
            sanitized_referrer = re.sub(r"[^a-zA-Z0-9.-]", "_", domain)

        # GeoIP (falls back to "Unknown" when DB unavailable or lookup fails)
        country = await self._geoip.get_country(client_ip)
        city = await self._geoip.get_city(client_ip) or cf_city

        redirect_ms = context.redirect_ms

        # Bot detection
        is_bot = is_bot_request(user_agent)
        bot_name = get_bot_name(user_agent) if is_bot else None

        if url_data.block_bots and is_bot:
            log.info(
                "bot_blocked",
                short_code=short_code,
                bot_name=bot_name or "generic",
                schema="v2",
            )
            return  # Skip analytics; redirect still proceeds

        # Build and insert ClickDoc
        url_id = ObjectId(url_data.id)
        owner_id = (
            ObjectId(url_data.owner_id) if url_data.owner_id else ANONYMOUS_OWNER_ID
        )

        curr_time = datetime.now(timezone.utc)
        click_doc = ClickDoc(
            clicked_at=curr_time,
            meta=ClickMeta(
                url_id=url_id,
                short_code=short_code,
                owner_id=owner_id,
                # Empty string from older cached entries → None so per-domain
                # queries can distinguish "unknown" from a real value.
                domain=url_data.domain or None,
            ),
            ip_address=client_ip,
            country=country or "Unknown",
            city=city or "Unknown",
            browser=browser,
            os=os_name,
            redirect_ms=redirect_ms,
            referrer=sanitized_referrer,
            bot_name=bot_name,
            device=device,
            utm_source=context.utm_source,
            utm_medium=context.utm_medium,
            utm_campaign=context.utm_campaign,
        )

        await self._click_repo.insert(click_doc.to_mongo())
        await self._url_repo.increment_clicks(url_id, last_click_time=curr_time)

        if should_sample("url_redirect"):
            log.info(
                "click_recorded",
                short_code=short_code,
                schema="v2",
                country=country or "Unknown",
                city=city or "Unknown",
                browser=browser,
                os=os_name,
                device=device,
                is_bot=is_bot,
                bot_name=bot_name,
                referrer_domain=sanitized_referrer,
                owner_id=str(url_data.owner_id) if url_data.owner_id else None,
                duration_ms=redirect_ms,
            )

        # Max-clicks expiry — atomic conditional update
        if url_data.max_clicks:
            expired = await self._url_repo.expire_if_max_clicks(
                url_id, url_data.max_clicks
            )
            if expired:
                log.info(
                    "url_expired",
                    url_id=str(url_id),
                    short_code=short_code,
                    reason="max_clicks_reached",
                    max_clicks=url_data.max_clicks,
                )
                await self._url_cache.invalidate(short_code, url_data.domain)
                await self._emit_expired(url_id)

    async def _emit_expired(self, url_id: ObjectId) -> None:
        """link.expired at discovery — the atomic expire above gates this
        to once per link. Fetches the doc (rare branch) for the snapshot."""
        if self._events is None:
            return
        # Function-local import: webhooks.payloads imports classify_device
        # from this module, so a top-level import would be a cycle.
        from services.webhooks.payloads import build_link_expired

        doc = await self._url_repo.find_by_id(url_id)
        if doc is None:
            return
        event = build_link_expired(doc, "max_clicks_reached")
        if event is not None:
            await self._events.emit(event)


class LegacyClickHandler:
    """Records a click for a v1 or emoji URL via embedded document update."""

    def __init__(
        self,
        legacy_repo: LegacyUrlRepository,
        emoji_repo: EmojiUrlRepository,
        geoip: GeoIPService,
    ) -> None:
        self._legacy_repo = legacy_repo
        self._emoji_repo = emoji_repo
        self._geoip = geoip

    async def handle(self, context: ClickContext) -> None:
        """
        Record a click for a v1 or emoji URL via embedded document update.

        Bot handling differs from v2: blocked bots raise ForbiddenError and
        the redirect is also blocked (not just analytics).

        Raises:
            ValidationError: Missing or unparseable User-Agent.
            ForbiddenError:  Bot blocked (v1 behavior — redirect is prevented).
        """
        url_data = context.url_data
        short_code = context.short_code
        is_emoji = context.is_emoji
        client_ip = context.client_ip
        user_agent = context.user_agent
        referrer = context.referrer

        if not user_agent:
            raise ValidationError("Invalid User-Agent")

        ua = ua_parse(user_agent)
        if not ua or not ua.user_agent or not ua.os:
            log.debug("ua_invalid", schema="v1", user_agent=user_agent[:200])
            raise ValidationError("Invalid User-Agent")

        os_name = ua.os.family
        browser = ua.user_agent.family

        # Referrer extraction (v1 style — less sanitization than v2)
        referrer_domain: str | None = None
        if referrer:
            domain = registrable_domain(referrer)
            referrer_domain = re.sub(r"[.$\x00-\x1F\x7F-\x9F]", "_", domain)

        # GeoIP
        country = await self._geoip.get_country(client_ip)
        if country:
            country = country.replace(".", " ")

        # Build update document
        updates: dict = {"$inc": {}, "$set": {}, "$addToSet": {}}

        if referrer_domain:
            updates["$inc"][f"referrer.{referrer_domain}.counts"] = 1
            updates["$addToSet"][f"referrer.{referrer_domain}.ips"] = client_ip

        updates["$inc"][f"country.{country}.counts"] = 1
        updates["$addToSet"][f"country.{country}.ips"] = client_ip
        updates["$inc"][f"browser.{browser}.counts"] = 1
        updates["$addToSet"][f"browser.{browser}.ips"] = client_ip
        updates["$inc"][f"os_name.{os_name}.counts"] = 1
        updates["$addToSet"][f"os_name.{os_name}.ips"] = client_ip

        # Bot detection (v1: blocked bot raises ForbiddenError)
        is_bot = is_bot_request(user_agent)
        bot_name: str | None = None
        if is_bot:
            bot_name = get_bot_name(user_agent)
            if url_data.block_bots:
                log.info(
                    "bot_blocked",
                    short_code=short_code,
                    bot_name=bot_name or "generic",
                    schema="v1",
                )
                raise ForbiddenError("Access Denied, Bots not allowed")
            if bot_name:
                sanitized_bot = re.sub(r"[.$\x00-\x1F\x7F-\x9F]", "_", str(bot_name))
                updates["$inc"][f"bots.{sanitized_bot}"] = 1

        # Daily counters
        today = str(datetime.now(timezone.utc)).split()[0]
        updates["$inc"][f"counter.{today}"] = 1

        # Unique click detection.
        # ips are not tracked in UrlCacheData — defaults to empty list,
        # meaning every click appears unique. This matches the cache-hit
        # behavior in the legacy redirector (where cached url_data also
        # lacks the ips field).
        updates["$inc"][f"unique_counter.{today}"] = 1

        updates["$addToSet"]["ips"] = client_ip
        updates["$inc"]["total-clicks"] = 1

        # Last click metadata
        current_time_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        updates["$set"]["last-click"] = current_time_str
        updates["$set"]["last-click-browser"] = browser
        updates["$set"]["last-click-os"] = os_name
        updates["$set"]["last-click-country"] = country

        # Average redirection time (exponential moving average, alpha=0.1)
        # curr_avg defaults to 0 since UrlCacheData doesn't carry this field
        redirection_time = float(context.redirect_ms)
        curr_avg = 0.0
        alpha = 0.1
        updates["$set"]["average_redirection_time"] = round(
            (1 - alpha) * curr_avg + alpha * redirection_time, 2
        )

        # Persist
        if is_emoji:
            await self._emoji_repo.update(short_code, updates)
        else:
            await self._legacy_repo.update(short_code, updates)

        if should_sample("url_redirect"):
            log.info(
                "click_recorded",
                short_code=short_code,
                schema="emoji" if is_emoji else "v1",
                country=country or "Unknown",
                browser=browser,
                os=os_name,
                is_bot=is_bot,
                bot_name=bot_name,
                duration_ms=int(redirection_time),
            )
