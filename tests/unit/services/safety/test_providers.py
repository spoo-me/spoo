"""Unit tests for the analysis providers."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from schemas.enums.safety import VerdictTier
from services.safety.providers import BlockedPatternProvider


class TestBlockedPatternProvider:
    @pytest.mark.asyncio
    async def test_pattern_match_is_toxic(self):
        repo = AsyncMock()
        repo.get_patterns = AsyncMock(return_value=[r"(?i)corr\.php"])
        verdict = await BlockedPatternProvider(repo, regex_timeout=0.2).analyze(
            "https://x.contabo.net/COR/corr.php", "x.contabo.net", "contabo.net"
        )
        assert verdict is not None
        assert verdict.tier == VerdictTier.TOXIC
        assert verdict.scope == "path_pattern"
        assert verdict.path_pattern == r"(?i)corr\.php"

    @pytest.mark.asyncio
    async def test_no_match_abstains(self):
        repo = AsyncMock()
        repo.get_patterns = AsyncMock(return_value=[r"(?i)corr\.php"])
        assert (
            await BlockedPatternProvider(repo, regex_timeout=0.2).analyze(
                "https://ok.com/x", "ok.com", "ok.com"
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_repo_error_abstains(self):
        repo = AsyncMock()
        repo.get_patterns = AsyncMock(side_effect=RuntimeError("mongo down"))
        assert (
            await BlockedPatternProvider(repo, regex_timeout=0.2).analyze(
                "https://ok.com/x", "ok.com", "ok.com"
            )
            is None
        )


class TestFeedDomainProvider:
    @pytest.mark.asyncio
    async def test_host_hit_is_toxic(self):
        from services.safety.providers import FeedDomainProvider

        repo = AsyncMock()
        repo.contains = AsyncMock(return_value=True)
        provider = FeedDomainProvider(repo, feed="fishfish", reason_label="fishfish.gg")
        verdict = await provider.analyze("https://evil.com/x", "evil.com", "evil.com")
        assert verdict is not None
        assert verdict.tier == VerdictTier.TOXIC
        assert verdict.reason == "host evil.com is listed by fishfish.gg"
        repo.contains.assert_awaited_once_with("fishfish", "evil.com")

    @pytest.mark.asyncio
    async def test_registrable_fallback_when_host_clean(self):
        from services.safety.providers import FeedDomainProvider

        repo = AsyncMock()
        repo.contains = AsyncMock(side_effect=[False, True])
        provider = FeedDomainProvider(repo, feed="fishfish", reason_label="fishfish.gg")
        verdict = await provider.analyze(
            "https://a.evil.com/x", "a.evil.com", "evil.com"
        )
        assert verdict is not None
        assert verdict.reason == "domain evil.com is listed by fishfish.gg"

    @pytest.mark.asyncio
    async def test_exempt_subdomain_host_abstains_on_a_registrable_hit(self):
        from services.safety.providers import FeedDomainProvider

        feeds = {"shorteners": {"goo.gl"}, "redirectors": {"maps.app.goo.gl"}}
        repo = AsyncMock()
        repo.contains = AsyncMock(side_effect=lambda feed, d: d in feeds[feed])
        provider = FeedDomainProvider(
            repo, feed="shorteners", reason_label="x", exempt_feed="redirectors"
        )
        assert (
            await provider.analyze(
                "https://maps.app.goo.gl/a", "maps.app.goo.gl", "goo.gl"
            )
            is None
        )
        assert (
            await provider.analyze("https://x.goo.gl/a", "x.goo.gl", "goo.gl")
            is not None
        )

    @pytest.mark.asyncio
    async def test_exempt_feed_is_not_read_on_a_miss(self):
        from services.safety.providers import FeedDomainProvider

        repo = AsyncMock()
        repo.contains = AsyncMock(return_value=False)
        provider = FeedDomainProvider(
            repo, feed="shorteners", reason_label="x", exempt_feed="redirectors"
        )
        assert (
            await provider.analyze("https://a.ok.com/x", "a.ok.com", "ok.com") is None
        )
        assert [c.args[0] for c in repo.contains.await_args_list] == [
            "shorteners",
            "shorteners",
        ]

    @pytest.mark.asyncio
    async def test_miss_and_error_abstain(self):
        from services.safety.providers import FeedDomainProvider

        repo = AsyncMock()
        repo.contains = AsyncMock(return_value=False)
        provider = FeedDomainProvider(repo, feed="fishfish", reason_label="fishfish.gg")
        assert await provider.analyze("https://ok.com/x", "ok.com", "ok.com") is None

        repo.contains = AsyncMock(side_effect=RuntimeError("mongo down"))
        assert await provider.analyze("https://ok.com/x", "ok.com", "ok.com") is None


class TestWebRiskProvider:
    @staticmethod
    def _http(payload, status=200):
        from types import SimpleNamespace

        http = AsyncMock()
        http.get = AsyncMock(
            return_value=SimpleNamespace(status_code=status, json=lambda: payload)
        )
        return http

    @pytest.mark.asyncio
    async def test_threat_match_is_toxic(self):
        from infrastructure.web_risk import ENFORCEMENT_THREAT_TYPES, WebRiskClient
        from services.safety.providers import WebRiskProvider

        http = self._http(
            {"threat": {"threatTypes": ["SOCIAL_ENGINEERING"], "expireTime": "x"}}
        )
        provider = WebRiskProvider(
            WebRiskClient(http, api_key="k123", threat_types=ENFORCEMENT_THREAT_TYPES)
        )
        verdict = await provider.analyze(
            "https://phish.com/x", "phish.com", "phish.com"
        )
        assert verdict is not None
        assert verdict.tier == VerdictTier.TOXIC
        assert "SOCIAL_ENGINEERING" in verdict.reason
        # Request carries the uri and both threat types, and the key
        # never rides the query string.
        _, kwargs = http.get.await_args
        assert kwargs["params"]["uri"] == "https://phish.com/x"
        assert kwargs["headers"]["X-Goog-Api-Key"] == "k123"
        assert "key" not in kwargs["params"]
        assert set(kwargs["params"]["threatTypes"]) == {
            "MALWARE",
            "SOCIAL_ENGINEERING",
        }

    @pytest.mark.asyncio
    async def test_empty_response_abstains(self):
        from infrastructure.web_risk import ENFORCEMENT_THREAT_TYPES, WebRiskClient
        from services.safety.providers import WebRiskProvider

        provider = WebRiskProvider(
            WebRiskClient(
                self._http({}), api_key="k123", threat_types=ENFORCEMENT_THREAT_TYPES
            )
        )
        assert await provider.analyze("https://ok.com/x", "ok.com", "ok.com") is None

    @pytest.mark.asyncio
    async def test_http_error_and_exception_abstain(self):
        from infrastructure.web_risk import ENFORCEMENT_THREAT_TYPES, WebRiskClient
        from services.safety.providers import WebRiskProvider

        provider = WebRiskProvider(
            WebRiskClient(
                self._http({}, status=429),
                api_key="k123",
                threat_types=ENFORCEMENT_THREAT_TYPES,
            )
        )
        assert await provider.analyze("https://ok.com/x", "ok.com", "ok.com") is None

        http = AsyncMock()
        http.get = AsyncMock(side_effect=RuntimeError("boom"))
        provider = WebRiskProvider(
            WebRiskClient(http, api_key="k123", threat_types=ENFORCEMENT_THREAT_TYPES)
        )
        assert await provider.analyze("https://ok.com/x", "ok.com", "ok.com") is None


class TestBlockedPatternProviderCache:
    @pytest.mark.asyncio
    async def test_patterns_cached_within_ttl(self):
        repo = AsyncMock()
        repo.get_patterns = AsyncMock(return_value=[r"evil\.com"])
        provider = BlockedPatternProvider(
            repo, regex_timeout=0.2, patterns_ttl_seconds=60
        )

        for _ in range(5):
            await provider.analyze("https://ok.com/x", "ok.com", "ok.com")

        repo.get_patterns.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ttl_zero_disables_cache(self):
        repo = AsyncMock()
        repo.get_patterns = AsyncMock(return_value=[])
        provider = BlockedPatternProvider(
            repo, regex_timeout=0.2, patterns_ttl_seconds=0
        )

        await provider.analyze("https://ok.com/x", "ok.com", "ok.com")
        await provider.analyze("https://ok.com/x", "ok.com", "ok.com")

        assert repo.get_patterns.await_count == 2


class TestToxicVerdictProvider:
    @pytest.mark.asyncio
    async def test_toxic_verdict_gates_creation(self):
        from datetime import datetime, timezone

        from schemas.models.verdict import VerdictDoc
        from services.safety.providers import ToxicVerdictProvider

        repo = AsyncMock()
        repo.find_by_host = AsyncMock(
            return_value=VerdictDoc(
                host="evil.com",
                tier=VerdictTier.TOXIC,
                reason="listed by fishfish.gg",
                updated_at=datetime.now(timezone.utc),
            )
        )
        verdict = await ToxicVerdictProvider(repo).analyze(
            "https://evil.com/new-kit", "evil.com", "evil.com"
        )
        assert verdict is not None
        assert verdict.tier == VerdictTier.TOXIC
        assert "previously judged malicious" in verdict.reason

    @pytest.mark.asyncio
    async def test_uncertain_verdict_and_miss_abstain(self):
        from datetime import datetime, timezone

        from schemas.models.verdict import VerdictDoc
        from services.safety.providers import ToxicVerdictProvider

        repo = AsyncMock()
        repo.find_by_host = AsyncMock(
            return_value=VerdictDoc(
                host="gray.com",
                tier=VerdictTier.UNCERTAIN,
                updated_at=datetime.now(timezone.utc),
            )
        )
        provider = ToxicVerdictProvider(repo)
        assert (
            await provider.analyze("https://gray.com/x", "gray.com", "gray.com") is None
        )

        repo.find_by_host = AsyncMock(return_value=None)
        assert await provider.analyze("https://ok.com/x", "ok.com", "ok.com") is None

    @pytest.mark.asyncio
    async def test_repo_error_abstains(self):
        from services.safety.providers import ToxicVerdictProvider

        repo = AsyncMock()
        repo.find_by_host = AsyncMock(side_effect=RuntimeError("mongo down"))
        assert (
            await ToxicVerdictProvider(repo).analyze(
                "https://ok.com/x", "ok.com", "ok.com"
            )
            is None
        )


class TestToxicVerdictScope:
    """The gate honours the verdict's scope: narrow judgments refuse narrowly."""

    def _repo(self, **fields) -> AsyncMock:
        from datetime import datetime, timezone

        from schemas.models.verdict import VerdictDoc

        repo = AsyncMock()
        repo.find_by_host = AsyncMock(
            return_value=VerdictDoc(
                host="sites.google.com",
                tier=VerdictTier.TOXIC,
                reason="phishing kit on one site",
                updated_at=datetime.now(timezone.utc),
                **fields,
            )
        )
        return repo

    @pytest.mark.asyncio
    async def test_pattern_scope_refuses_only_matching_urls(self):
        from services.safety.providers import ToxicVerdictProvider

        repo = self._repo(
            scope="path_pattern",
            path_pattern=r"^https://sites\.google\.com/view/evil/.*",
        )
        provider = ToxicVerdictProvider(repo)
        hit = await provider.analyze(
            "https://sites.google.com/view/evil/login",
            "sites.google.com",
            "google.com",
        )
        assert hit is not None and hit.tier == VerdictTier.TOXIC
        assert (
            await provider.analyze(
                "https://sites.google.com/view/school-club/home",
                "sites.google.com",
                "google.com",
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_links_scope_refuses_only_the_judged_url(self):
        from services.safety.providers import ToxicVerdictProvider

        repo = self._repo(
            scope="links", sample_url="https://sites.google.com/view/evil/login"
        )
        provider = ToxicVerdictProvider(repo)
        assert (
            await provider.analyze(
                "https://sites.google.com/view/evil/login",
                "sites.google.com",
                "google.com",
            )
            is not None
        )
        assert (
            await provider.analyze(
                "https://sites.google.com/view/other/page",
                "sites.google.com",
                "google.com",
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_absent_scope_means_host_wide(self):
        from services.safety.providers import ToxicVerdictProvider

        repo = self._repo()
        assert (
            await ToxicVerdictProvider(repo).analyze(
                "https://sites.google.com/anything",
                "sites.google.com",
                "google.com",
            )
            is not None
        )


class TestSharedCarrierLookup:
    """e.vg sits on a scam feed for what routed through it. This lookup is
    how a host-wide block on such a domain gets flagged rather than passing
    silently."""

    FEEDS = ("shorteners", "redirectors")

    @classmethod
    def _lookup(cls, listed: set[str]):
        """The mock keys on (feed, domain) like the real repository, so a
        lookup against a feed this instance was never configured with is a
        miss rather than a silent pass."""
        from services.safety.providers import SharedCarrierLookup

        repo = AsyncMock()
        repo.contains = AsyncMock(
            side_effect=lambda feed, domain: feed in cls.FEEDS and domain in listed
        )
        return SharedCarrierLookup(repo, feeds=cls.FEEDS), repo

    @pytest.mark.asyncio
    async def test_listed_host_is_covered(self):
        lookup, _repo = self._lookup({"e.vg"})
        assert await lookup.covers("e.vg", "e.vg") is True

    @pytest.mark.asyncio
    async def test_registrable_domain_matches_when_the_host_does_not(self):
        lookup, _repo = self._lookup({"bitly.cx"})
        assert await lookup.covers("sub.bitly.cx", "bitly.cx") is True

    @pytest.mark.asyncio
    async def test_unlisted_host_is_not_covered(self):
        lookup, repo = self._lookup({"e.vg"})
        assert await lookup.covers("rbxtools.st", "rbxtools.st") is False
        # Apex: host and registrable domain are the same read, once per feed.
        assert [c.args for c in repo.contains.await_args_list] == [
            ("shorteners", "rbxtools.st"),
            ("redirectors", "rbxtools.st"),
        ]

    @pytest.mark.asyncio
    async def test_a_subdomain_checks_both_forms_against_every_feed(self):
        lookup, repo = self._lookup(set())
        assert await lookup.covers("sub.rbxtools.st", "rbxtools.st") is False
        assert [c.args for c in repo.contains.await_args_list] == [
            ("shorteners", "sub.rbxtools.st"),
            ("shorteners", "rbxtools.st"),
            ("redirectors", "sub.rbxtools.st"),
            ("redirectors", "rbxtools.st"),
        ]

    @pytest.mark.asyncio
    async def test_a_feed_it_was_not_configured_with_is_never_consulted(self):
        lookup, repo = self._lookup({"e.vg"})
        await lookup.covers("e.vg", "e.vg")
        assert {c.args[0] for c in repo.contains.await_args_list} <= set(self.FEEDS)

    @pytest.mark.asyncio
    async def test_blank_registrable_domain_is_skipped(self):
        lookup, repo = self._lookup(set())
        assert await lookup.covers("e.vg", "") is False
        assert [c.args for c in repo.contains.await_args_list] == [
            ("shorteners", "e.vg"),
            ("redirectors", "e.vg"),
        ]

    @pytest.mark.asyncio
    async def test_backend_failure_reports_not_covered(self):
        """A dead feed store must not invent a carrier, and must not raise
        into the enforcement path either."""
        from services.safety.providers import SharedCarrierLookup

        repo = AsyncMock()
        repo.contains = AsyncMock(side_effect=RuntimeError("mongo down"))
        lookup = SharedCarrierLookup(repo, feeds=("shorteners",))

        assert await lookup.covers("e.vg", "e.vg") is False
