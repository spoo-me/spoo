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
from typing import Protocol

from infrastructure.http_client import HttpClient
from infrastructure.logging import get_logger
from repositories.blocked_url_repository import BlockedUrlRepository
from repositories.feed_domain_repository import FeedDomainRepository
from repositories.verdict_repository import VerdictRepository
from schemas.enums.safety import VerdictTier
from shared.validators import matching_blocked_pattern

log = get_logger(__name__)


@dataclass(frozen=True)
class ProviderVerdict:
    tier: VerdictTier
    reason: str
    # "host", "links" (one exact URL) or "path_pattern" (regex in
    # ``path_pattern``): how far the evidence reaches, not what it enforces.
    scope: str = "host"
    path_pattern: str | None = None


class AnalysisProvider(Protocol):
    name: str

    async def analyze(
        self, url: str, host: str, registrable_domain: str
    ) -> ProviderVerdict | None: ...


class FeedDomainProvider:
    """Membership check against a synced external feed's domain set
    (``safety_feed_domains``). An empty or never-synced set abstains — the
    feed layer is additive signal, never a gate on its own health."""

    def __init__(
        self, repo: FeedDomainRepository, *, feed: str, reason_label: str
    ) -> None:
        self._repo = repo
        self._feed = feed
        self._label = reason_label
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
    """Google Web Risk Lookup API (``uris:search``) — judges the full URL
    against Google's MALWARE and SOCIAL_ENGINEERING lists. Online lookup
    (100k/month free tier covers report-triggered volume by orders of
    magnitude); the local hash-DB variant is a later create-gate concern.

    Network or quota failures abstain.
    """

    name = "web_risk"

    _THREAT_TYPES = ("MALWARE", "SOCIAL_ENGINEERING")

    def __init__(
        self,
        http_client: HttpClient,
        *,
        api_key: str,
        api_base: str = "https://webrisk.googleapis.com",
    ) -> None:
        self._http = http_client
        self._api_key = api_key
        self._api_base = api_base.rstrip("/")

    async def analyze(
        self, url: str, host: str, registrable_domain: str
    ) -> ProviderVerdict | None:
        try:
            # Key rides a header: httpx logs full request URLs at INFO.
            response = await self._http.get(
                f"{self._api_base}/v1/uris:search",
                params={
                    "uri": url,
                    "threatTypes": list(self._THREAT_TYPES),
                },
                headers={"X-Goog-Api-Key": self._api_key},
                timeout=10.0,
            )
            if response.status_code != 200:
                log.warning(
                    "safety_provider_failed",
                    provider=self.name,
                    error=f"http {response.status_code}",
                )
                return None
            threat = response.json().get("threat")
            if threat:
                types = ",".join(threat.get("threatTypes", [])) or "UNKNOWN"
                return ProviderVerdict(
                    tier=VerdictTier.TOXIC,
                    reason=f"flagged by Google Web Risk ({types})",
                    scope="links",
                )
        except Exception as exc:
            log.warning(
                "safety_provider_failed",
                provider=self.name,
                error=type(exc).__name__,
            )
        return None


def verdict_covers(verdict, url: str) -> bool:
    """Does this verdict's scope cover *url*?"""
    scope = verdict.scope or "host"
    if scope == "host":
        return True
    if scope == "path_pattern" and verdict.path_pattern:
        return matching_blocked_pattern(url, (verdict.path_pattern,)) is not None
    if scope == "links" and verdict.sample_url:
        return _without_query(url) == _without_query(verdict.sample_url)
    return False


def _without_query(url: str) -> str:
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
