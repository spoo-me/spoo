"""Unit tests for UrlPolicyService — the L0 create/edit gate."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from schemas.enums.safety import VerdictTier
from services.safety.policy import UrlPolicyService
from services.safety.providers import (
    BlockedPatternProvider,
    FeedDomainProvider,
    ProviderVerdict,
)


class _StubProvider:
    def __init__(self, verdict: ProviderVerdict | None, name: str = "stub"):
        self._verdict = verdict
        self.name = name
        self.calls: list[tuple[str, str, str]] = []

    async def analyze(self, url, host, registrable_domain):
        self.calls.append((url, host, registrable_domain))
        return self._verdict


class TestFormatAndSelfLink:
    @pytest.mark.asyncio
    async def test_invalid_url_rejected_with_precise_message(self):
        gate = UrlPolicyService([], blocked_self_domains=["spoo.me"])
        rejection = await gate.check("not a url")
        assert rejection is not None
        assert rejection.code == "invalid_url"
        assert rejection.public_message == "URL is not allowed or invalid"

    @pytest.mark.asyncio
    async def test_self_link_rejected(self):
        gate = UrlPolicyService([], blocked_self_domains=["spoo.me"])
        assert await gate.check("https://spoo.me/abc") is not None

    @pytest.mark.asyncio
    async def test_valid_url_with_no_providers_passes(self):
        gate = UrlPolicyService([], blocked_self_domains=["spoo.me"])
        assert await gate.check("https://example.com/x") is None


class TestProviderChain:
    @pytest.mark.asyncio
    async def test_toxic_provider_blocks_with_coarse_message(self):
        provider = _StubProvider(
            ProviderVerdict(tier=VerdictTier.TOXIC, reason="listed by fishfish.gg"),
            name="feed_fishfish",
        )
        gate = UrlPolicyService([provider], blocked_self_domains=["spoo.me"])

        rejection = await gate.check("https://a.evil.com/kit")

        assert rejection is not None
        assert rejection.code == "feed_fishfish"
        # Coarse on the wire: the precise reason must never leak.
        assert rejection.public_message == "URL is blocked"
        assert "fishfish" not in rejection.public_message
        # Providers receive the parsed destination parts.
        assert provider.calls == [("https://a.evil.com/kit", "a.evil.com", "evil.com")]

    @pytest.mark.asyncio
    async def test_abstaining_providers_pass(self):
        gate = UrlPolicyService(
            [_StubProvider(None), _StubProvider(None)],
            blocked_self_domains=["spoo.me"],
        )
        assert await gate.check("https://example.com/x") is None

    @pytest.mark.asyncio
    async def test_first_toxic_provider_short_circuits(self):
        first = _StubProvider(
            ProviderVerdict(tier=VerdictTier.TOXIC, reason="a"), name="first"
        )
        second = _StubProvider(
            ProviderVerdict(tier=VerdictTier.TOXIC, reason="b"), name="second"
        )
        gate = UrlPolicyService([first, second], blocked_self_domains=["spoo.me"])

        rejection = await gate.check("https://evil.com/x")

        assert rejection.code == "first"
        assert second.calls == []


class TestGateWithRealProviders:
    """The gate over the real providers — the composition the wiring builds."""

    @pytest.mark.asyncio
    async def test_pattern_and_feed_block_at_create_time(self):
        pattern_repo = AsyncMock()
        pattern_repo.get_patterns = AsyncMock(return_value=[r"(?i)corr\.php"])
        feed_repo = AsyncMock()
        feed_repo.contains = AsyncMock(
            side_effect=lambda feed, domain: domain == "scam.net"
        )
        gate = UrlPolicyService(
            [
                BlockedPatternProvider(
                    pattern_repo, regex_timeout=0.2, patterns_ttl_seconds=0
                ),
                FeedDomainProvider(
                    feed_repo, feed="fishfish", reason_label="fishfish.gg"
                ),
            ],
            blocked_self_domains=["spoo.me"],
        )

        blocked_by_pattern = await gate.check("https://x.contabo.net/COR/corr.php")
        assert blocked_by_pattern.code == "blocked_pattern"

        blocked_by_feed = await gate.check("https://scam.net/login")
        assert blocked_by_feed.code == "feed_fishfish"

        assert await gate.check("https://example.com/fine") is None

    @pytest.mark.asyncio
    async def test_provider_failure_fails_open(self):
        pattern_repo = AsyncMock()
        pattern_repo.get_patterns = AsyncMock(side_effect=RuntimeError("mongo down"))
        gate = UrlPolicyService(
            [
                BlockedPatternProvider(
                    pattern_repo, regex_timeout=0.2, patterns_ttl_seconds=0
                )
            ],
            blocked_self_domains=["spoo.me"],
        )
        # A broken provider must never take down link creation.
        assert await gate.check("https://example.com/x") is None


class TestPublicMessageOverride:
    @pytest.mark.asyncio
    async def test_published_policy_gets_helpful_message(self):
        provider = _StubProvider(
            ProviderVerdict(tier=VerdictTier.TOXIC, reason="chain refusal"),
            name="feed_shorteners",
        )
        gate = UrlPolicyService(
            [provider],
            blocked_self_domains=["spoo.me"],
            public_messages={
                "feed_shorteners": "Links to other URL shorteners are not allowed"
            },
        )
        rejection = await gate.check("https://bit.ly/abc")
        assert rejection.public_message == (
            "Links to other URL shorteners are not allowed"
        )

    @pytest.mark.asyncio
    async def test_security_blocks_keep_coarse_default(self):
        provider = _StubProvider(
            ProviderVerdict(tier=VerdictTier.TOXIC, reason="x"), name="feed_fishfish"
        )
        gate = UrlPolicyService(
            [provider],
            blocked_self_domains=["spoo.me"],
            public_messages={"feed_shorteners": "friendly"},
        )
        rejection = await gate.check("https://evil.com/x")
        assert rejection.public_message == "URL is blocked"


class TestRedirectProbe:
    def _policy(self, *, contains: bool):
        from services.safety.policy import UrlPolicyService

        feed_repo = AsyncMock()
        feed_repo.contains = AsyncMock(return_value=contains)
        sink = AsyncMock()
        policy = UrlPolicyService(
            [],
            blocked_self_domains=["spoo.me"],
            redirect_feed_repo=feed_repo,
            redirect_sink=sink,
        )
        return policy, sink

    @pytest.mark.asyncio
    async def test_redirector_destination_emits_a_redirect_event(self):
        policy, sink = self._policy(contains=True)
        await policy.record_create("https://t.co/AbCdEf")
        event = sink.emit.await_args.args[0]
        assert event.trigger == "redirect"
        assert event.host == "t.co"

    @pytest.mark.asyncio
    async def test_ordinary_destination_emits_nothing(self):
        policy, sink = self._policy(contains=False)
        await policy.record_create("https://example.com/x")
        sink.emit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_deps_degrade_to_a_noop(self):
        from services.safety.policy import UrlPolicyService

        policy = UrlPolicyService([], blocked_self_domains=["spoo.me"])
        await policy.record_create("https://t.co/AbCdEf")
