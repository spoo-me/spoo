"""Cross-tier tests: screening, admission, investigation and enforcement
wired together against ONE shared verdict store — the seams where the bugs
lived. Only the model call and the outbound edges are doubles.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from schemas.enums.safety import VerdictTier
from schemas.models.verdict import VerdictDoc
from services.safety.admission import AdmissionPolicy
from services.safety.analyzer import SafetyAnalyzer
from services.safety.events import SafetyAnalyzeEvent
from services.safety.investigation import (
    AutoBlockPolicy,
    Classification,
    Confidence,
    DeepInvestigator,
    InvestigationVerdict,
    Scope,
)
from services.safety.providers import BlockedPatternProvider, ToxicVerdictProvider


class FakeVerdictRepo:
    """Dict-backed verdict store with real upsert semantics (absent scope = untouched)."""

    def __init__(self) -> None:
        self.docs: dict[str, VerdictDoc] = {}

    async def upsert_verdict(
        self,
        host,
        *,
        registrable_domain,
        tier,
        reason,
        source,
        trigger,
        sample_url=None,
        context=None,
        decided_by="system",
        scope=None,
        path_pattern=None,
        provenance=None,
    ) -> None:
        fields = {
            "host": host,
            "registrable_domain": registrable_domain,
            "tier": tier,
            "reason": reason,
            "source": source,
            "trigger": trigger,
            "sample_url": sample_url,
            "context": context,
            "decided_by": decided_by,
            "updated_at": datetime.now(timezone.utc),
        }
        if scope is not None:
            fields["scope"] = scope
            fields["path_pattern"] = path_pattern
        if provenance:
            fields.update(provenance)
        existing = self.docs.get(host)
        if existing is not None:
            merged = existing.model_dump()
            merged.update(fields)
            self.docs[host] = VerdictDoc(**merged)
        else:
            self.docs[host] = VerdictDoc(**fields)

    async def find_by_host(self, host):
        return self.docs.get(host)


class CapturingDeepSink:
    def __init__(self) -> None:
        self.events: list[SafetyAnalyzeEvent] = []

    async def emit(self, event: SafetyAnalyzeEvent) -> None:
        self.events.append(event)


def _pattern_provider(pattern: str) -> BlockedPatternProvider:
    repo = AsyncMock()
    repo.get_patterns = AsyncMock(return_value=[pattern])
    return BlockedPatternProvider(repo, regex_timeout=0.2, patterns_ttl_seconds=0)


def _admission() -> AdmissionPolicy:
    redis = AsyncMock()
    redis.incr = AsyncMock(return_value=1)
    redis.expire = AsyncMock()
    return AdmissionPolicy(redis, daily_budget=50, report_daily_budget=50)


def _enforcer() -> AsyncMock:
    enforcer = AsyncMock()
    enforcer.block_host = AsyncMock(
        return_value=AsyncMock(blocked_count=3, legacy_count=1)
    )
    enforcer.block_matching = AsyncMock(
        return_value=AsyncMock(blocked_count=1, legacy_count=0)
    )
    enforcer.block_aliases = AsyncMock(return_value=AsyncMock(blocked_count=1))
    return enforcer


def _analyzer(providers, verdict_repo, enforcer, sink):
    return SafetyAnalyzer(
        providers,
        verdict_repo,
        enforcer,
        AsyncMock(),
        reverdict_ttl_hours=24,
        admission=_admission(),
        deep_sink=sink,
    )


def _investigator(verdict_repo, enforcer, model_verdict) -> DeepInvestigator:
    runner = AsyncMock()
    runner.run = AsyncMock(return_value=model_verdict)
    url_repo = AsyncMock()
    url_repo.destination_history = AsyncMock(
        return_value={
            "link_count": 5,
            "anon_count": 5,
            "owned_count": 0,
            "distinct_owners": 0,
            "total_clicks": 10,
            "first_seen": "2026-08-01T00:00:00+00:00",
            "edited_count": 0,
        }
    )
    return DeepInvestigator(
        runner,
        AsyncMock(versioned_prompt="v1+test"),
        url_repo,
        verdict_repo,
        enforcer,
        AsyncMock(),
        policy=AutoBlockPolicy.CORROBORATED,
        model_name="anthropic:test",
    )


def _event(
    url: str, host: str, trigger: str = "report", screening: str | None = None
) -> SafetyAnalyzeEvent:
    context = {"screening": screening} if screening else None
    return SafetyAnalyzeEvent(
        url=url, host=host, registrable_domain=host, trigger=trigger, context=context
    )


class TestScreeningToInvestigationToReenforcement:
    @pytest.mark.asyncio
    async def test_pattern_hit_travels_the_whole_ladder(self):
        """Narrow block, escalate, widen to host, then re-enforce host-wide."""
        store = FakeVerdictRepo()
        enforcer = _enforcer()
        sink = CapturingDeepSink()
        analyzer = _analyzer(
            [_pattern_provider(r"^https://evil-kit\.example/.*")],
            store,
            enforcer,
            sink,
        )

        # 1. Screening: narrow block + narrow verdict + escalation.
        await analyzer.analyze(
            _event("https://evil-kit.example/login", "evil-kit.example")
        )
        enforcer.block_host.assert_not_awaited()
        enforcer.block_matching.assert_awaited_once()
        assert store.docs["evil-kit.example"].scope == "path_pattern"
        assert len(sink.events) == 1
        escalated = sink.events[0]
        assert "blocked_pattern" in escalated.context["screening"]

        # 2. Investigation widens to host; screening context corroborates.
        inv = _investigator(
            store,
            enforcer,
            InvestigationVerdict(
                classification=Classification.SCAM_HOST,
                confidence=Confidence.HIGH,
                reason="every path is the same kit",
                scope=Scope.HOST,
                scope_justification="single-purpose domain",
            ),
        )
        await inv.investigate(escalated)
        enforcer.block_host.assert_awaited_once()
        assert store.docs["evil-kit.example"].tier == VerdictTier.TOXIC
        assert store.docs["evil-kit.example"].scope == "host"

        # 3. A later event re-enforces host-wide without running providers.
        await analyzer.analyze(
            _event("https://evil-kit.example/other", "evil-kit.example")
        )
        assert enforcer.block_host.await_count == 2
        assert len(sink.events) == 1  # no second escalation

    @pytest.mark.asyncio
    async def test_narrow_investigation_verdict_survives_the_next_report(self):
        """The L2 scope-escalation bug: a path_pattern verdict never widens."""
        store = FakeVerdictRepo()
        enforcer = _enforcer()
        sink = CapturingDeepSink()

        inv = _investigator(
            store,
            enforcer,
            InvestigationVerdict(
                classification=Classification.SCAM_HOST,
                confidence=Confidence.HIGH,
                reason="phishing confined to one site",
                scope=Scope.PATH_PATTERN,
                path_pattern=r"^https://sites\.shared\.example/view/evil/.*",
                scope_justification="shared platform, 200 creators",
            ),
        )
        # Corroborated the way a real escalation is: via the event context.
        await inv.investigate(
            _event(
                "https://sites.shared.example/view/evil/home",
                "sites.shared.example",
                screening="blocked_pattern: destination matches a pattern",
            )
        )
        assert store.docs["sites.shared.example"].tier == VerdictTier.TOXIC
        assert store.docs["sites.shared.example"].scope == "path_pattern"
        enforcer.block_host.assert_not_awaited()

        # Screening sees a report about an INNOCENT page on the platform.
        analyzer = _analyzer(
            [_pattern_provider(r"^\\bnever-matches\\b$")], store, enforcer, sink
        )
        await analyzer.analyze(
            _event(
                "https://sites.shared.example/view/school-club/home",
                "sites.shared.example",
            )
        )
        enforcer.block_host.assert_not_awaited()

        # The create gate refuses covered URLs and only covered URLs.
        gate = ToxicVerdictProvider(store)
        assert (
            await gate.analyze(
                "https://sites.shared.example/view/evil/new",
                "sites.shared.example",
                "shared.example",
            )
            is not None
        )
        assert (
            await gate.analyze(
                "https://sites.shared.example/view/chess-club/home",
                "sites.shared.example",
                "shared.example",
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_investigation_failure_leaves_the_host_reviewable(self):
        """Deep-tier breakage degrades to uncertain + review, never a silent pass."""
        from infrastructure.llm import LlmTaskFailed

        store = FakeVerdictRepo()
        enforcer = _enforcer()
        inv = _investigator(store, enforcer, None)
        inv._runner.run = AsyncMock(
            side_effect=LlmTaskFailed("safety-investigate", "timeout")
        )

        await inv.investigate(_event("https://flaky.example/x", "flaky.example"))
        assert store.docs["flaky.example"].tier == VerdictTier.UNCERTAIN
        enforcer.block_host.assert_not_awaited()
        enforcer.block_matching.assert_not_awaited()


class TestRedirectCrossTier:
    @pytest.mark.asyncio
    async def test_wrapper_event_ends_with_terminal_verdict_and_wrapper_kill(self):
        store = FakeVerdictRepo()
        enforcer = _enforcer()
        sink = CapturingDeepSink()
        analyzer = _analyzer(
            [_pattern_provider(r"^https://landing-scam\.example/.*")],
            store,
            enforcer,
            sink,
        )

        with patch(
            "services.safety.resolver.resolve_terminal_url",
            AsyncMock(return_value="https://landing-scam.example/kit"),
        ):
            await analyzer.analyze(
                _event("https://t.co/AbCdEf", "t.co", trigger="redirect")
            )

        # Terminal host judged and stored; wrapper host never judged.
        assert store.docs["landing-scam.example"].tier == VerdictTier.TOXIC
        assert "t.co" not in store.docs
        # Terminal narrow block + wrapper kill = two matching blocks.
        assert enforcer.block_matching.await_count == 2
        hosts = [c.args[0] for c in enforcer.block_matching.await_args_list]
        assert hosts == ["landing-scam.example", "t.co"]
        # The terminal escalated to the deep queue for the host decision.
        assert [e.host for e in sink.events] == ["landing-scam.example"]
