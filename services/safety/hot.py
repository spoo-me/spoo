"""Hot-link screening — the abuse consumer of the hotness detector.

A hot link on an unverdicted destination host gets a screening event:
the activation-time catch for campaigns seeded quietly and distributed
after every create-time check has come and gone.
"""

from __future__ import annotations

from infrastructure.logging import get_logger
from repositories.legacy.legacy_url_repository import LegacyUrlRepository
from repositories.url_repository import UrlRepository
from repositories.verdict_repository import VerdictRepository
from services.click.consumers.hotness import HotUrl
from services.safety.events import SafetyAnalyzeEvent
from services.safety.sinks import SafetySink
from shared.url_utils import parse_destination

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
            url = await self._destination(hot)
            if not url:
                return
            parts = parse_destination(url)
            if parts is None:
                return
            if await self._verdict_repo.find_by_host(parts["host"]) is not None:
                return
            await self._sink.emit(
                SafetyAnalyzeEvent(
                    url=url,
                    host=parts["host"],
                    registrable_domain=parts["registrable_domain"],
                    trigger="hot",
                    context={
                        "short_code": hot.short_code,
                        "domain": hot.domain,
                        "clicks_in_window": hot.count,
                    },
                )
            )
            log.info("safety_hot_screening", host=parts["host"], alias=hot.short_code)
        except Exception as exc:
            log.warning(
                "safety_hot_screen_failed",
                short_code=hot.short_code,
                error=str(exc),
                error_type=type(exc).__name__,
            )

    async def _destination(self, hot: HotUrl) -> str | None:
        domain = self._system_domain if hot.domain in ("default", "") else hot.domain
        doc = await self._url_repo.find_by_alias(hot.short_code, domain)
        if doc is not None:
            return doc.long_url
        legacy = await self._legacy_repo.find_by_id(hot.short_code)
        return legacy.url if legacy is not None else None
