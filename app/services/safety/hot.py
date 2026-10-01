"""Hot-link screening — the abuse consumer of the hotness detector.

A hot link on an unverdicted destination host gets a screening event:
the activation-time catch for campaigns seeded quietly and distributed
after every create-time check has come and gone.
"""

from __future__ import annotations

from app.infrastructure.logging import get_logger
from app.repositories.legacy.legacy_url_repository import LegacyUrlRepository
from app.repositories.url_repository import UrlRepository
from app.repositories.verdict_repository import VerdictRepository
from app.services.click.consumers.hotness import HotUrl
from app.services.safety.events import SafetyAnalyzeEvent
from app.services.safety.sinks import SafetySink
from app.shared.url_utils import (
    link_destination_urls,
    link_destination_urls_for,
    parse_destination,
)

log = get_logger(__name__)


class HotLinkScreen:
    """HotUrlAction: emit a screening event for hot links on unverdicted
    destination hosts. Best-effort like every hotness action."""

    def __init__(
        self,
        url_repo: UrlRepository,
        legacy_repo: LegacyUrlRepository,
        verdict_repo: VerdictRepository,
        sink: SafetySink,
        *,
        system_default_domain: str = "spoo.me",
    ) -> None:
        self._url_repo = url_repo
        self._legacy_repo = legacy_repo
        self._verdict_repo = verdict_repo
        self._sink = sink
        self._system_domain = system_default_domain

    async def on_hot(self, hot: HotUrl) -> None:
        try:
            urls = await self._destinations(hot)
            seen: set[str] = set()
            for url in urls:
                parts = parse_destination(url)
                if parts is None or parts["host"] in seen:
                    continue
                seen.add(parts["host"])
                if await self._verdict_repo.find_by_host(parts["host"]) is not None:
                    continue
                context = {
                    "short_code": hot.short_code,
                    "domain": hot.domain,
                    "clicks_in_window": hot.count,
                }
                if len(urls) > 1:
                    context["link_destinations"] = urls
                await self._sink.emit(
                    SafetyAnalyzeEvent(
                        url=url,
                        host=parts["host"],
                        registrable_domain=parts["registrable_domain"],
                        trigger="hot",
                        context=context,
                    )
                )
                log.info(
                    "safety_hot_screening", host=parts["host"], alias=hot.short_code
                )
        except Exception as exc:
            log.warning(
                "safety_hot_screen_failed",
                short_code=hot.short_code,
                error=str(exc),
                error_type=type(exc).__name__,
            )

    async def _destinations(self, hot: HotUrl) -> list[str]:
        """Every destination the link routes to: main, geo overrides, variants, both fallbacks."""
        domain = self._system_domain if hot.domain in ("default", "") else hot.domain
        doc = await self._url_repo.find_by_alias(hot.short_code, domain)
        if doc is not None:
            return link_destination_urls_for(doc)
        legacy = await self._legacy_repo.find_by_id(hot.short_code)
        return link_destination_urls(legacy.url) if legacy is not None else []
