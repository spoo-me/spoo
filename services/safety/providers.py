"""Analysis providers — the pluggable judgment sources.

Each provider inspects one signal and either returns a verdict or
abstains (None). Providers must never raise on bad input or backend
failure: safety analysis is best-effort by contract and a broken provider
degrades to abstention, not to a stuck queue. The chain is ordered; the
first non-abstaining provider wins.

v1 ships the local sources (operator blocklists). The deep tier (page
render + LLM) plugs in here as another provider without touching the
pipeline.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal, Protocol

from infrastructure.logging import get_logger
from infrastructure.web_risk import WebRiskClient
from repositories.blocked_url_repository import BlockedUrlRepository
from repositories.feed_domain_repository import FeedDomainRepository
from repositories.verdict_repository import VerdictRepository
from schemas.enums.safety import VerdictTier
from shared.validators import matching_blocked_pattern

log = get_logger(__name__)


VerdictScope = Literal["host", "links", "path_pattern"]


@dataclass(frozen=True)
class ProviderVerdict:
    tier: VerdictTier
    reason: str
    # How far the evidence reaches, not what it enforces.
    scope: VerdictScope = "host"
    path_pattern: str | None = None


class AnalysisProvider(Protocol):
    name: str

    async def analyze(
        self, url: str, host: str, registrable_domain: str
    ) -> ProviderVerdict | None: ...


class SharedCarrierLookup:
    """Membership check for feeds whose domains CARRY other people's links.

    A shortener or a platform share wrapper appears on a scam feed because
    scammers routed through it, never because the domain itself is the
    scam. Enforcement still follows the feed and blocks host-wide; this
    lookup only marks the blocks that therefore reached every unrelated
    link routed through the same domain.
    """

    def __init__(self, repo: FeedDomainRepository, *, feeds: tuple[str, ...]) -> None:
        self._repo = repo
        self._feeds = feeds

    async def covers(self, host: str, registrable_domain: str) -> bool:
        """Never raises. The block and the verdict are already written by
        the time this runs, so a raise could not widen or narrow them. What
        it would take out is the operator notification on the next line, and
        a block nobody hears about is worse than an unflagged one."""
        candidates = [d for d in dict.fromkeys((host, registrable_domain)) if d]
        for feed in self._feeds:
            for domain in candidates:
                try:
                    if await self._repo.contains(feed, domain):
                        return True
                except Exception as exc:
                    log.warning(
                        "shared_carrier_lookup_failed",
                        feed=feed,
                        domain=domain,
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                    return False
        return False


class FeedDomainProvider:
    """Membership check against a synced external feed's domain set
    (``safety_feed_domains``). An empty or never-synced set abstains — the
    feed layer is additive signal, never a gate on its own health."""

    def __init__(
        self,
        repo: FeedDomainRepository,
        *,
        feed: str,
        reason_label: str,
        exempt_feed: str | None = None,
    ) -> None:
        self._repo = repo
        self._feed = feed
        self._label = reason_label
        self._exempt_feed = exempt_feed
        self.name = f"feed_{feed}"

    async def analyze(
        self, url: str, host: str, registrable_domain: str
    ) -> ProviderVerdict | None:
        try:
            if await self._repo.contains(self._feed, host):
                return ProviderVerdict(
                    tier=VerdictTier.TOXIC,
                    reason=f"host {host} is listed by {self._label}",
                    scope="host",
                )
            if (
                registrable_domain
                and registrable_domain != host
                and await self._repo.contains(self._feed, registrable_domain)
                and not (
                    self._exempt_feed
                    and await self._repo.contains(self._exempt_feed, host)
                )
            ):
                return ProviderVerdict(
                    tier=VerdictTier.TOXIC,
                    reason=f"domain {registrable_domain} is listed by {self._label}",
                    scope="host",
                )
        except Exception as exc:
            log.warning("safety_provider_failed", provider=self.name, error=str(exc))
        return None


class WebRiskProvider:
    """Google Web Risk verdict for the full URL. Online lookup; the local
    hash-DB variant is a later create-gate concern.

    The 100k/month free tier no longer belongs to reports alone: the public
    URL expander spends the same project quota, which is why its share is
    capped and this consumer's is not.

    An unanswered lookup abstains.
    """

    name = "web_risk"

    def __init__(self, client: WebRiskClient) -> None:
        self._client = client

    async def analyze(
        self, url: str, host: str, registrable_domain: str
    ) -> ProviderVerdict | None:
        threats = await self._client.lookup(url)
        if not threats:
            return None
        return ProviderVerdict(
            tier=VerdictTier.TOXIC,
            reason=f"flagged by Google Web Risk ({','.join(threats)})",
            scope="links",
        )


def verdict_covers(verdict, url: str) -> bool:
    """Does this verdict's scope cover *url*?"""
    scope = verdict.scope or "host"
    if scope == "host":
        return True
    if scope == "path_pattern" and verdict.path_pattern:
        return matching_blocked_pattern(url, (verdict.path_pattern,)) is not None
    if scope == "links" and verdict.sample_url:
        return without_query(url) == without_query(verdict.sample_url)
    return False


def without_query(url: str) -> str:
    """Scheme, host and path only: a links-scoped verdict covers the judged
    URL, and appending ``?a=1`` is not a different destination."""
    return url.split("?", 1)[0].split("#", 1)[0].rstrip("/")


class ToxicVerdictProvider:
    """The verdict store as a gate source: a destination ANY analysis tier
    has judged toxic (report-triggered today, deep/L2 later) refuses new
    link creation instantly — one verdict write powers analysis dedupe,
    click-time enforcement AND the create gate, with no copying into
    other lists. One indexed point read per create.

    The verdict's scope bounds the refusal — a toxic page on a shared
    platform never turns the whole platform away."""

    name = "toxic_verdict"

    def __init__(self, repo: VerdictRepository) -> None:
        self._repo = repo

    async def analyze(
        self, url: str, host: str, registrable_domain: str
    ) -> ProviderVerdict | None:
        try:
            verdict = await self._repo.find_by_host(host)
            if (
                verdict is not None
                and verdict.tier is VerdictTier.TOXIC
                and verdict_covers(verdict, url)
            ):
                return ProviderVerdict(
                    tier=VerdictTier.TOXIC,
                    reason=(
                        f"destination previously judged malicious"
                        f" ({verdict.reason or 'no reason recorded'})"
                    ),
                )
        except Exception as exc:
            log.warning("safety_provider_failed", provider=self.name, error=str(exc))
        return None


class BlockedPatternProvider:
    """Regex blocklist (the ``blocked-urls`` collection) evaluated against
    the full destination URL. One instance is shared by the create gate and
    the analyzer, so both moments see the same patterns AND share the cache.

    The repository is uncached by contract ("caching is the service
    layer's job") — this provider IS that service layer: patterns are
    cached for ``patterns_ttl_seconds`` (0 disables), so the create path
    stops re-reading the whole collection per request while operator edits
    still go live within the TTL.
    """

    name = "blocked_pattern"

    def __init__(
        self,
        repo: BlockedUrlRepository,
        *,
        regex_timeout: float,
        patterns_ttl_seconds: float = 30.0,
    ) -> None:
        self._repo = repo
        self._timeout = regex_timeout
        self._ttl = patterns_ttl_seconds
        self._cached: list[str] | None = None
        self._fetched_at = 0.0

    async def _patterns(self) -> list[str]:
        now = time.monotonic()
        if (
            self._cached is None
            or self._ttl <= 0
            or now - self._fetched_at >= self._ttl
        ):
            self._cached = await self._repo.get_patterns()
            self._fetched_at = now
        return self._cached

    async def analyze(
        self, url: str, host: str, registrable_domain: str
    ) -> ProviderVerdict | None:
        try:
            patterns = await self._patterns()
            matched = matching_blocked_pattern(url, patterns, timeout=self._timeout)
            if matched is not None:
                return ProviderVerdict(
                    tier=VerdictTier.TOXIC,
                    reason="destination matches an operator blocklist pattern",
                    scope="path_pattern",
                    path_pattern=matched,
                )
        except Exception as exc:
            log.warning("safety_provider_failed", provider=self.name, error=str(exc))
        return None
