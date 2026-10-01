"""Consumer for meta.image.validate events (framework-free, like
StatsClickConsumer): fetch the external og:image with SSRF guards, record
its dimensions, or clear it on hard failure.

Outcome semantics:
  - malformed payload → drop (never poison the group)
  - FetchDeniedError (401/403 to OUR UA) → keep the image, skip dims —
    preview crawlers fetch with their own allowlisted UAs
  - FetchHardError → CAS-clear the image + invalidate cache + KV re-sync
  - FetchTransientError → raise (no XACK → pending → claimer retries →
    ClaimDeadLetterGuard DLQs after max_deliveries)
  - success → CAS-record {width,height,bytes,content_type} + invalidate
    + KV re-sync so the page gains og:image:width/height

All persistence is CAS-filtered on the image URL: a user edit racing the
worker is never clobbered.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from bson import ObjectId

from app.infrastructure.cache.url_cache import UrlCacheData
from app.infrastructure.logging import get_logger
from app.infrastructure.safe_fetch import (
    DEFAULT_USER_AGENT,
    FetchDeniedError,
    FetchHardError,
    fetch_public_image,
)
from app.services.meta_tags.events import meta_image_event_from_payload
from app.shared.image_sniff import sniff_image

if TYPE_CHECKING:
    from app.infrastructure.cache.url_cache import UrlCache
    from app.repositories.url_repository import UrlRepository
    from app.services.edge_cache.og_writethrough import OgEdgeWritethrough

log = get_logger(__name__)


class MetaImageValidator:
    def __init__(
        self,
        url_repo: UrlRepository,
        url_cache: UrlCache,
        *,
        og_writethrough: OgEdgeWritethrough | None = None,
        timeout: float = 5.0,
        max_bytes: int = 1_048_576,
        max_redirects: int = 3,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self._url_repo = url_repo
        self._url_cache = url_cache
        self._og_writethrough = og_writethrough
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._user_agent = user_agent

    async def consume(self, payload: Any) -> None:
        event = meta_image_event_from_payload(payload)
        if event is None:
            return

        try:
            fetched = await fetch_public_image(
                event.image_url,
                timeout=self._timeout,
                max_bytes=self._max_bytes,
                max_redirects=self._max_redirects,
                user_agent=self._user_agent,
            )
        except FetchDeniedError as exc:
            # 401/403 means the host blocked OUR validator UA (WAF, hotlink
            # protection) — real preview crawlers fetch with their own,
            # widely-allowlisted UAs and may render the image fine. Keep the
            # user's image; we just can't record dimensions.
            log.info(
                "meta_image_validation_denied",
                url_id=event.url_id,
                reason=str(exc),
            )
            return
        except FetchHardError as exc:
            changed = await self._url_repo.clear_meta_image(
                ObjectId(event.url_id), event.image_url
            )
            if changed:
                await self._invalidate(event.alias, event.domain)
            log.warning(
                "meta_image_cleared",
                url_id=event.url_id,
                reason=str(exc),
                cas_applied=changed,
            )
            return
        # FetchTransientError propagates → retry via the claimer.

        info = sniff_image(fetched.data)
        meta = {
            "width": info.width if info else None,
            "height": info.height if info else None,
            "bytes": len(fetched.data),
            "content_type": fetched.content_type,
            "checked_at": datetime.now(timezone.utc),
        }
        changed = await self._url_repo.record_meta_image_validation(
            ObjectId(event.url_id), event.image_url, meta
        )
        if changed:
            await self._invalidate(event.alias, event.domain)
        log.info(
            "meta_image_validated",
            url_id=event.url_id,
            bytes=meta["bytes"],
            width=meta["width"],
            height=meta["height"],
            cas_applied=changed,
        )

    async def _invalidate(self, alias: str, domain: str) -> None:
        await self._url_cache.invalidate(alias, domain)
        if self._og_writethrough is None:
            return
        # Re-render the edge entry from fresh DB state (the cache was just
        # invalidated, so a cache read would miss).
        doc = await self._url_repo.find_by_alias(alias, domain)
        if doc is not None:
            await self._og_writethrough.sync(UrlCacheData.from_v2_doc(doc))
