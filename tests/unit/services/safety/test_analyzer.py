"""Unit tests for SafetyAnalyzer orchestration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from schemas.enums.safety import VerdictTier
from schemas.models.verdict import VerdictDoc
from services.safety.analyzer import SafetyAnalyzer
from services.safety.events import SafetyAnalyzeEvent
from services.safety.providers import ProviderVerdict


def _event(host="evil.com") -> SafetyAnalyzeEvent:
    return SafetyAnalyzeEvent(
        url=f"https://{host}/kit",
        host=host,
        registrable_domain=host,
        trigger="report",
        context={"report_count": 1},
    )


class _Provider:
    def __init__(self, verdict: ProviderVerdict | None, name="stub"):
        self._verdict = verdict
        self.name = name
        self.calls = 0

    async def analyze(self, url, host, registrable_domain):
        self.calls += 1
        return self._verdict


def _build(providers, existing=None):
    verdict_repo = AsyncMock()
    verdict_repo.find_by_host = AsyncMock(return_value=existing)
    enforcer = AsyncMock()
    enforcer.block_host = AsyncMock(
        return_value=AsyncMock(blocked_count=4, legacy_count=1)
    )
    notifier = AsyncMock()
    analyzer = SafetyAnalyzer(
        providers, verdict_repo, enforcer, notifier, reverdict_ttl_hours=24
    )
    return analyzer, verdict_repo, enforcer, notifier


class TestAnalyze:
    @pytest.mark.asyncio
    async def test_url_scoped_signal_blocks_only_the_judged_url(self):
        """A signal that judged one URL enforces one URL."""
        provider = _Provider(
            ProviderVerdict(
                tier=VerdictTier.TOXIC, reason="web risk hit", scope="links"
            ),
            name="web_risk",
        )
        analyzer, verdict_repo, enforcer, notifier = _build([provider])

        await analyzer.analyze(_event())

        kwargs = verdict_repo.upsert_verdict.await_args.kwargs
        assert kwargs["tier"] == VerdictTier.TOXIC
        assert kwargs["source"] == "web_risk"
        assert kwargs["scope"] == "links"
        enforcer.block_host.assert_not_awaited()
        enforcer.block_matching.assert_awaited_once()
        notifier.safety_action.assert_awaited_once()
        notifier.safety_review.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_host_scoped_feed_hit_blocks_the_host(self):
        """A feed listing the HOST is a host-level hard call; enforcing it
        per-URL would leave every other link to a listed domain serving."""
        provider = _Provider(
            ProviderVerdict(
                tier=VerdictTier.TOXIC,
                reason="host evil.com is listed by fishfish.gg",
                scope="host",
            ),
            name="feed_fishfish",
        )
        analyzer, verdict_repo, enforcer, _n = _build([provider])

        await analyzer.analyze(_event())

        enforcer.block_host.assert_awaited_once()
        assert verdict_repo.upsert_verdict.await_args.kwargs["scope"] == "host"

    @pytest.mark.asyncio
    async def test_unusable_model_pattern_degrades_to_the_judged_url(self):
        """A pattern that cannot compile is never stored or executed: the
        verdict lands links-scoped instead of carrying a landmine."""
        provider = _Provider(
            ProviderVerdict(
                tier=VerdictTier.TOXIC,
                reason="pattern hit",
                scope="path_pattern",
                path_pattern=r"^https://evil\.com/(unclosed",
            ),
            name="blocked_pattern",
        )
        analyzer, verdict_repo, enforcer, _n = _build([provider])

        await analyzer.analyze(_event())

        kwargs = verdict_repo.upsert_verdict.await_args.kwargs
        assert kwargs["scope"] == "links"
        assert kwargs["path_pattern"] is None
        enforcer.block_host.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pattern_hit_blocks_matching_links_and_stores_the_pattern(self):
        pattern = r"^https://sites\.google\.com/view/evil/.*"
        provider = _Provider(
            ProviderVerdict(
                tier=VerdictTier.TOXIC,
                reason="operator blocklist pattern",
                scope="path_pattern",
                path_pattern=pattern,
            ),
            name="blocked_pattern",
        )
        analyzer, verdict_repo, enforcer, _notifier = _build([provider])

        await analyzer.analyze(
            SafetyAnalyzeEvent(
                url="https://sites.google.com/view/evil/page",
                host="sites.google.com",
                registrable_domain="google.com",
                trigger="report",
            )
        )

        kwargs = verdict_repo.upsert_verdict.await_args.kwargs
        assert kwargs["scope"] == "path_pattern"
        assert kwargs["path_pattern"] == pattern
        enforcer.block_host.assert_not_awaited()
        matcher = enforcer.block_matching.await_args.kwargs["matcher"]
        assert matcher("https://sites.google.com/view/evil/other")
        assert not matcher("https://sites.google.com/view/school-club/home")

    @pytest.mark.asyncio
    async def test_all_abstain_records_uncertain_and_requests_review(self):
        analyzer, verdict_repo, enforcer, notifier = _build([_Provider(None)])

        await analyzer.analyze(_event())

        kwargs = verdict_repo.upsert_verdict.await_args.kwargs
        assert kwargs["tier"] == VerdictTier.UNCERTAIN
        enforcer.block_host.assert_not_awaited()
        notifier.safety_review.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_existing_toxic_verdict_reenforces_without_reanalysis(self):
        provider = _Provider(None)
        existing = VerdictDoc(
            host="evil.com",
            tier=VerdictTier.TOXIC,
            reason="old verdict",
            updated_at=datetime.now(timezone.utc) - timedelta(days=30),
        )
        analyzer, verdict_repo, enforcer, _notifier = _build([provider], existing)

        await analyzer.analyze(_event())

        assert provider.calls == 0
        enforcer.block_host.assert_awaited_once()
        verdict_repo.upsert_verdict.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fresh_nontoxic_verdict_short_circuits(self):
        provider = _Provider(None)
        existing = VerdictDoc(
            host="evil.com",
            tier=VerdictTier.UNCERTAIN,
            updated_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        analyzer, verdict_repo, _enforcer, notifier = _build([provider], existing)

        await analyzer.analyze(_event())

        assert provider.calls == 0
        verdict_repo.upsert_verdict.assert_not_awaited()
        notifier.safety_review.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stale_uncertain_verdict_reanalyzes(self):
        provider = _Provider(None)
        existing = VerdictDoc(
            host="evil.com",
            tier=VerdictTier.UNCERTAIN,
            updated_at=datetime.now(timezone.utc) - timedelta(days=3),
        )
        analyzer, verdict_repo, _enforcer, _notifier = _build([provider], existing)

        await analyzer.analyze(_event())

        assert provider.calls == 1
        verdict_repo.upsert_verdict.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_first_provider_wins(self):
        first = _Provider(
            ProviderVerdict(tier=VerdictTier.TOXIC, reason="a"), name="first"
        )
        second = _Provider(
            ProviderVerdict(tier=VerdictTier.TOXIC, reason="b"), name="second"
        )
        analyzer, verdict_repo, _, _ = _build([first, second])

        await analyzer.analyze(_event())

        assert first.calls == 1
        assert second.calls == 0
        assert verdict_repo.upsert_verdict.await_args.kwargs["source"] == "first"


class TestHumanVerdicts:
    @pytest.mark.asyncio
    async def test_human_benign_verdict_never_goes_stale(self):
        """The allowlist: a human marking a popular domain benign silences
        its recurring bursts permanently, however old the verdict is."""
        provider = _Provider(None)
        existing = VerdictDoc(
            host="youtube.com",
            tier=VerdictTier.BENIGN,
            decided_by="human:zingzy",
            updated_at=datetime.now(timezone.utc) - timedelta(days=365),
        )
        analyzer, verdict_repo, enforcer, notifier = _build([provider], existing)

        await analyzer.analyze(_event("youtube.com"))

        assert provider.calls == 0
        verdict_repo.upsert_verdict.assert_not_awaited()
        enforcer.block_host.assert_not_awaited()
        notifier.safety_review.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_human_toxic_verdict_still_enforces(self):
        existing = VerdictDoc(
            host="evil.com",
            tier=VerdictTier.TOXIC,
            decided_by="human:zingzy",
            reason="confirmed phishing",
            updated_at=datetime.now(timezone.utc) - timedelta(days=90),
        )
        analyzer, _verdict_repo, enforcer, _notifier = _build(
            [_Provider(None)], existing
        )

        await analyzer.analyze(_event())

        enforcer.block_host.assert_awaited_once()


class TestSweepNotificationPolicy:
    @pytest.mark.asyncio
    async def test_sweep_abstention_is_silent(self):
        """Screening coverage must not ping review for every innocent new
        destination — the uncertain verdict is the record."""
        analyzer, verdict_repo, _enforcer, notifier = _build([_Provider(None)])

        await analyzer.analyze(
            SafetyAnalyzeEvent(
                url="https://fresh.com/x",
                host="fresh.com",
                registrable_domain="fresh.com",
                trigger="sweep",
                context={"sweep": "recent_screen"},
            )
        )

        verdict_repo.upsert_verdict.assert_awaited_once()
        notifier.safety_review.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sweep_toxic_hit_still_notifies_action(self):
        provider = _Provider(
            ProviderVerdict(tier=VerdictTier.TOXIC, reason="listed by fishfish.gg"),
            name="feed_fishfish",
        )
        analyzer, _verdict_repo, enforcer, notifier = _build([provider])

        await analyzer.analyze(
            SafetyAnalyzeEvent(
                url="https://evil.com/x",
                host="evil.com",
                registrable_domain="evil.com",
                trigger="sweep",
                context={"sweep": "feed_delta", "feed": "fishfish"},
            )
        )

        enforcer.block_host.assert_awaited_once()
        notifier.safety_action.assert_awaited_once()


class TestDeepAdmission:
    """Unresolved screenings cross into investigation only through the
    admission policy — and an admitted event must not also ping review."""

    def _deep_build(self, *, admitted: bool, reason: str = "always"):
        from services.safety.admission import AdmissionDecision

        verdict_repo = AsyncMock()
        verdict_repo.find_by_host = AsyncMock(return_value=None)
        enforcer = AsyncMock()
        notifier = AsyncMock()
        admission = AsyncMock()
        admission.decide = AsyncMock(
            return_value=AdmissionDecision(admitted=admitted, reason=reason)
        )
        deep_sink = AsyncMock()
        analyzer = SafetyAnalyzer(
            [],
            verdict_repo,
            enforcer,
            notifier,
            reverdict_ttl_hours=24,
            admission=admission,
            deep_sink=deep_sink,
        )
        return analyzer, verdict_repo, notifier, deep_sink

    @pytest.mark.asyncio
    async def test_admitted_event_goes_deep_and_skips_the_review_embed(self):
        analyzer, verdict_repo, notifier, deep_sink = self._deep_build(admitted=True)

        await analyzer.analyze(_event())

        # The uncertain verdict is still written — investigation upgrades
        # it later; screening's record never depends on the deep tier.
        assert verdict_repo.upsert_verdict.await_args.kwargs["tier"] == (
            VerdictTier.UNCERTAIN
        )
        deep_sink.emit.assert_awaited_once()
        notifier.safety_review.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_denied_report_still_reaches_review(self):
        analyzer, _, notifier, deep_sink = self._deep_build(
            admitted=False, reason="budget_exhausted"
        )

        await analyzer.analyze(_event())

        deep_sink.emit.assert_not_awaited()
        notifier.safety_review.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_denied_sweep_stays_silent(self):
        analyzer, _, notifier, deep_sink = self._deep_build(
            admitted=False, reason="sweep_excluded"
        )

        sweep_event = SafetyAnalyzeEvent(
            url="https://evil.com/kit",
            host="evil.com",
            registrable_domain="evil.com",
            trigger="sweep",
        )
        await analyzer.analyze(sweep_event)

        deep_sink.emit.assert_not_awaited()
        notifier.safety_review.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_toxic_escalates_the_host_decision_to_investigation(self):
        """A screening hit blocks narrowly and escalates the host question."""
        from services.safety.admission import AdmissionDecision

        provider = _Provider(
            ProviderVerdict(tier=VerdictTier.TOXIC, reason="feed hit", scope="links"),
            name="feed_fishfish",
        )
        verdict_repo = AsyncMock()
        verdict_repo.find_by_host = AsyncMock(return_value=None)
        enforcer = AsyncMock()
        enforcer.block_matching = AsyncMock(
            return_value=AsyncMock(blocked_count=1, legacy_count=0)
        )
        admission = AsyncMock()
        admission.decide = AsyncMock(
            return_value=AdmissionDecision(admitted=True, reason="within_budget")
        )
        deep_sink = AsyncMock()
        notifier = AsyncMock()
        analyzer = SafetyAnalyzer(
            [provider],
            verdict_repo,
            enforcer,
            notifier,
            reverdict_ttl_hours=24,
            admission=admission,
            deep_sink=deep_sink,
        )

        await analyzer.analyze(_event())

        enforcer.block_host.assert_not_awaited()
        assert admission.decide.await_args.kwargs == {"escalation": True}
        deep_sink.emit.assert_awaited_once()
        emitted = deep_sink.emit.await_args.args[0]
        assert "feed_fishfish: feed hit" in emitted.context["screening"]
        assert "needs review" not in notifier.safety_action.await_args.kwargs["reason"]

    @pytest.mark.asyncio
    async def test_toxic_without_deep_tier_flags_the_host_decision_for_review(self):
        provider = _Provider(
            ProviderVerdict(tier=VerdictTier.TOXIC, reason="feed hit"),
            name="feed_fishfish",
        )
        analyzer, _verdict_repo, _enforcer, notifier = _build([provider])

        await analyzer.analyze(_event())

        reason = notifier.safety_action.await_args.kwargs["reason"]
        assert "needs review" in reason


class TestScopedReenforcement:
    """Stored verdicts re-enforce within their scope, never wider."""

    @pytest.mark.asyncio
    async def test_pattern_scoped_verdict_covers_a_matching_url(self):
        provider = _Provider(None)
        existing = VerdictDoc(
            host="sites.google.com",
            tier=VerdictTier.TOXIC,
            reason="phishing kit on one site",
            scope="path_pattern",
            path_pattern=r"^https://sites\.google\.com/view/evil/.*",
            updated_at=datetime.now(timezone.utc) - timedelta(days=30),
        )
        analyzer, verdict_repo, enforcer, _n = _build([provider], existing)

        await analyzer.analyze(
            SafetyAnalyzeEvent(
                url="https://sites.google.com/view/evil/page2",
                host="sites.google.com",
                registrable_domain="google.com",
                trigger="report",
            )
        )

        enforcer.block_host.assert_not_awaited()
        enforcer.block_matching.assert_awaited_once()
        assert provider.calls == 0
        verdict_repo.upsert_verdict.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pattern_scoped_verdict_lets_an_uncovered_url_reanalyze(self):
        provider = _Provider(None)
        existing = VerdictDoc(
            host="sites.google.com",
            tier=VerdictTier.TOXIC,
            reason="phishing kit on one site",
            scope="path_pattern",
            path_pattern=r"^https://sites\.google\.com/view/evil/.*",
            updated_at=datetime.now(timezone.utc) - timedelta(days=30),
        )
        analyzer, verdict_repo, enforcer, _n = _build([provider], existing)

        await analyzer.analyze(
            SafetyAnalyzeEvent(
                url="https://sites.google.com/view/school-club/home",
                host="sites.google.com",
                registrable_domain="google.com",
                trigger="report",
            )
        )

        enforcer.block_host.assert_not_awaited()
        assert provider.calls == 1
        assert (
            verdict_repo.upsert_verdict.await_args.kwargs["tier"]
            == VerdictTier.UNCERTAIN
        )


class TestTriggerAuthority:
    @pytest.mark.asyncio
    async def test_report_reanalyzes_a_sweep_screened_host(self):
        """A report inside the re-verdict TTL must re-analyze, not be swallowed."""
        provider = _Provider(None)
        existing = VerdictDoc(
            host="evil.com",
            tier=VerdictTier.UNCERTAIN,
            trigger="sweep",
            updated_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        analyzer, verdict_repo, _e, _n = _build([provider], existing)

        await analyzer.analyze(_event())

        assert provider.calls == 1
        verdict_repo.upsert_verdict.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sweep_never_reanalyzes_a_fresh_report_verdict(self):
        provider = _Provider(None)
        existing = VerdictDoc(
            host="evil.com",
            tier=VerdictTier.UNCERTAIN,
            trigger="report",
            updated_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        analyzer, verdict_repo, _e, _n = _build([provider], existing)

        await analyzer.analyze(
            SafetyAnalyzeEvent(
                url="https://evil.com/x",
                host="evil.com",
                registrable_domain="evil.com",
                trigger="sweep",
            )
        )

        assert provider.calls == 0
        verdict_repo.upsert_verdict.assert_not_awaited()


class TestReenforcementNotify:
    @pytest.mark.asyncio
    async def test_reenforcement_that_blocks_new_links_notifies(self):
        existing = VerdictDoc(
            host="evil.com",
            tier=VerdictTier.TOXIC,
            reason="old verdict",
            updated_at=datetime.now(timezone.utc) - timedelta(days=30),
        )
        analyzer, _v, enforcer, notifier = _build([_Provider(None)], existing)
        enforcer.block_host = AsyncMock(
            return_value=AsyncMock(blocked_count=3, legacy_count=1)
        )

        await analyzer.analyze(_event())

        reason = notifier.safety_action.await_args.kwargs["reason"]
        assert "re-enforced" in reason

    @pytest.mark.asyncio
    async def test_idempotent_reenforcement_stays_quiet(self):
        existing = VerdictDoc(
            host="evil.com",
            tier=VerdictTier.TOXIC,
            reason="old verdict",
            updated_at=datetime.now(timezone.utc) - timedelta(days=30),
        )
        analyzer, _v, enforcer, notifier = _build([_Provider(None)], existing)
        enforcer.block_host = AsyncMock(
            return_value=AsyncMock(blocked_count=0, legacy_count=0)
        )

        await analyzer.analyze(_event())

        notifier.safety_action.assert_not_awaited()


class TestRedirectScreening:
    """trigger="redirect": judge where the chain LANDS, never the wrapper."""

    def _wrapper_event(self):
        return SafetyAnalyzeEvent(
            url="https://t.co/AbCdEf",
            host="t.co",
            registrable_domain="t.co",
            trigger="redirect",
        )

    @pytest.mark.asyncio
    async def test_toxic_terminal_blocks_the_wrapped_link_too(self):
        from unittest.mock import patch

        provider = _Provider(
            ProviderVerdict(tier=VerdictTier.TOXIC, reason="feed hit", scope="links"),
            name="feed_fishfish",
        )
        verdict_repo = AsyncMock()
        # First read: no verdict; the wrapper-kill read then sees the toxic write.
        stored = VerdictDoc(
            host="evil-landing.com",
            tier=VerdictTier.TOXIC,
            reason="feed hit",
            scope="links",
            sample_url="https://evil-landing.com/kit",
            updated_at=datetime.now(timezone.utc),
        )
        verdict_repo.find_by_host = AsyncMock(side_effect=[None, stored])
        enforcer = AsyncMock()
        enforcer.block_matching = AsyncMock(
            return_value=AsyncMock(blocked_count=1, legacy_count=0)
        )
        notifier = AsyncMock()
        analyzer = SafetyAnalyzer(
            [provider], verdict_repo, enforcer, notifier, reverdict_ttl_hours=24
        )

        with patch(
            "services.safety.resolver.resolve_terminal_url",
            AsyncMock(return_value="https://evil-landing.com/kit"),
        ):
            await analyzer.analyze(self._wrapper_event())

        assert (
            verdict_repo.upsert_verdict.await_args.kwargs["sample_url"]
            == "https://evil-landing.com/kit"
        )
        wrapper_call = enforcer.block_matching.await_args_list[-1]
        assert wrapper_call.args[0] == "t.co"
        matcher = wrapper_call.kwargs["matcher"]
        assert matcher("https://t.co/AbCdEf")
        assert not matcher("https://t.co/other")
        hosts_written = [c.args[0] for c in verdict_repo.upsert_verdict.await_args_list]
        assert "t.co" not in hosts_written

    @pytest.mark.asyncio
    async def test_clean_terminal_stays_silent(self):
        from unittest.mock import patch

        verdict_repo = AsyncMock()
        verdict_repo.find_by_host = AsyncMock(return_value=None)
        enforcer = AsyncMock()
        notifier = AsyncMock()
        analyzer = SafetyAnalyzer(
            [_Provider(None)], verdict_repo, enforcer, notifier, reverdict_ttl_hours=24
        )

        with patch(
            "services.safety.resolver.resolve_terminal_url",
            AsyncMock(return_value="https://normal-blog.example/post"),
        ):
            await analyzer.analyze(self._wrapper_event())

        enforcer.block_matching.assert_not_awaited()
        enforcer.block_host.assert_not_awaited()
        # Machine-volume trigger: unresolved screening pings nobody.
        notifier.safety_review.assert_not_awaited()
        assert verdict_repo.upsert_verdict.await_args.kwargs["tier"] == (
            VerdictTier.UNCERTAIN
        )
        assert verdict_repo.upsert_verdict.await_args.args[0] == "normal-blog.example"

    @pytest.mark.asyncio
    async def test_unresolvable_chain_does_nothing(self):
        from unittest.mock import patch

        verdict_repo = AsyncMock()
        verdict_repo.find_by_host = AsyncMock(return_value=None)
        enforcer = AsyncMock()
        analyzer = SafetyAnalyzer(
            [], verdict_repo, enforcer, AsyncMock(), reverdict_ttl_hours=24
        )

        with patch(
            "services.safety.resolver.resolve_terminal_url",
            AsyncMock(return_value=None),
        ):
            await analyzer.analyze(self._wrapper_event())

        verdict_repo.upsert_verdict.assert_not_awaited()
        enforcer.block_matching.assert_not_awaited()


class TestHotTrigger:
    @pytest.mark.asyncio
    async def test_hot_unresolved_screening_is_silent(self):
        analyzer, verdict_repo, _e, notifier = _build([_Provider(None)])

        await analyzer.analyze(
            SafetyAnalyzeEvent(
                url="https://viral.example/post",
                host="viral.example",
                registrable_domain="viral.example",
                trigger="hot",
            )
        )

        verdict_repo.upsert_verdict.assert_awaited_once()
        notifier.safety_review.assert_not_awaited()
