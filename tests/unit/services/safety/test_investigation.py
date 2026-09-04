"""Unit tests for L2 investigation — the authority mapper (safety-critical
pure function) and the investigator flow."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from infrastructure.llm import LlmTaskFailed
from schemas.enums.safety import VerdictTier
from services.safety.events import SafetyAnalyzeEvent
from services.safety.investigation import (
    AutoBlockPolicy,
    Classification,
    Confidence,
    DeepInvestigator,
    InvestigationVerdict,
    Scope,
    decide_authority,
)


def _verdict(cls, conf=Confidence.HIGH, scope=Scope.HOST, **kw) -> InvestigationVerdict:
    return InvestigationVerdict(
        classification=cls,
        confidence=conf,
        reason="because",
        scope=scope,
        **kw,
    )


class TestAuthorityMapper:
    """Blast radius is the axis: self-limiting verdicts apply themselves;
    anything reaching future links waits for a human."""

    def test_corroborated_high_scam_blocks_host(self):
        d = decide_authority(
            _verdict(Classification.SCAM_HOST),
            corroborated=True,
            policy=AutoBlockPolicy.CORROBORATED,
        )
        assert d.action == "block_host" and d.auto and d.tier == VerdictTier.TOXIC

    def test_uncorroborated_scam_goes_to_review_under_default(self):
        d = decide_authority(
            _verdict(Classification.SCAM_HOST),
            corroborated=False,
            policy=AutoBlockPolicy.CORROBORATED,
        )
        assert d.action == "review" and not d.auto

    def test_compromised_legit_never_blocks_the_host(self):
        d = decide_authority(
            _verdict(Classification.COMPROMISED_LEGIT, scope=Scope.LINKS),
            corroborated=True,
            policy=AutoBlockPolicy.CORROBORATED,
        )
        assert d.action == "block_aliases" and d.auto

    def test_a_proposed_list_add_always_needs_a_human_tap(self):
        # Even fully corroborated and high-confidence: a list add reaches
        # every future link to the service.
        from services.safety.investigation import ListProposal

        d = decide_authority(
            _verdict(
                Classification.REDIRECTOR_SERVICE,
                proposals=[
                    ListProposal(list="shorteners", domain="cuted.xyz", why="wrapper")
                ],
            ),
            corroborated=True,
            policy=AutoBlockPolicy.CONFIDENT,
        )
        assert d.action == "propose" and not d.auto and d.tier == VerdictTier.GRAY

    def test_legit_relay_is_a_benign_verdict(self):
        d = decide_authority(
            _verdict(Classification.LEGIT_RELAY),
            corroborated=False,
            policy=AutoBlockPolicy.CORROBORATED,
        )
        assert d.action == "benign" and d.tier == VerdictTier.BENIGN

    def test_spam_gray_records_but_never_blocks(self):
        d = decide_authority(
            _verdict(Classification.SPAM_GRAY),
            corroborated=True,
            policy=AutoBlockPolicy.CONFIDENT,
        )
        assert d.action == "benign" and d.tier == VerdictTier.GRAY

    def test_uncertain_always_reviews(self):
        d = decide_authority(
            _verdict(Classification.UNCERTAIN, conf=Confidence.LOW),
            corroborated=True,
            policy=AutoBlockPolicy.CONFIDENT,
        )
        assert d.action == "review" and d.tier == VerdictTier.UNCERTAIN

    def test_confident_policy_blocks_model_alone(self):
        d = decide_authority(
            _verdict(Classification.SCAM_HOST),
            corroborated=False,
            policy=AutoBlockPolicy.CONFIDENT,
        )
        assert d.action == "block_host" and d.auto

    def test_confident_policy_still_needs_high_confidence(self):
        d = decide_authority(
            _verdict(Classification.SCAM_HOST, conf=Confidence.MEDIUM),
            corroborated=False,
            policy=AutoBlockPolicy.CONFIDENT,
        )
        assert d.action == "review"

    def test_both_policy_requires_confidence_and_corroboration(self):
        confident_only = decide_authority(
            _verdict(Classification.SCAM_HOST),
            corroborated=False,
            policy=AutoBlockPolicy.BOTH,
        )
        assert confident_only.action == "review"
        full = decide_authority(
            _verdict(Classification.SCAM_HOST),
            corroborated=True,
            policy=AutoBlockPolicy.BOTH,
        )
        assert full.action == "block_host"

    def test_off_policy_never_auto_blocks(self):
        d = decide_authority(
            _verdict(Classification.SCAM_HOST),
            corroborated=True,
            policy=AutoBlockPolicy.OFF,
        )
        assert d.action == "review" and not d.auto


def _event(trigger="report", screening=True, **ctx) -> SafetyAnalyzeEvent:
    """Default carries a screening finding so corroboration can be computed."""
    context = {"reasons": ["phishing"], "reported_codes": ["spoo.me/abc"], **ctx}
    if screening:
        context["screening"] = "blocked_pattern: destination matches a pattern"
    return SafetyAnalyzeEvent(
        url="https://evil.example/login",
        host="evil.example",
        registrable_domain="evil.example",
        trigger=trigger,
        context=context,
    )


def _investigator(
    verdict_or_exc, *, policy=AutoBlockPolicy.CORROBORATED, feed_repo=None
):
    runner = AsyncMock()
    if isinstance(verdict_or_exc, Exception):
        runner.run = AsyncMock(side_effect=verdict_or_exc)
    else:
        runner.run = AsyncMock(return_value=verdict_or_exc)
    url_repo = AsyncMock()
    url_repo.destination_history = AsyncMock(
        return_value={
            "link_count": 3,
            "anon_count": 3,
            "owned_count": 0,
            "distinct_owners": 0,
            "total_clicks": 40,
            "first_seen": "2026-08-01T00:00:00+00:00",
            "edited_count": 0,
        }
    )
    verdict_repo = AsyncMock()
    enforcer = AsyncMock()
    enforcer.block_host = AsyncMock(
        return_value=AsyncMock(blocked_count=3, legacy_count=1)
    )
    enforcer.block_aliases = AsyncMock(return_value=AsyncMock(blocked_count=1))
    enforcer.block_matching = AsyncMock(
        return_value=AsyncMock(blocked_count=2, legacy_count=0)
    )
    notifier = AsyncMock()
    inv = DeepInvestigator(
        runner,
        AsyncMock(versioned_prompt="v1+abcd1234"),
        url_repo,
        verdict_repo,
        enforcer,
        notifier,
        policy=policy,
        model_name="anthropic:claude-sonnet-5",
        feed_repo=feed_repo,
    )
    return inv, verdict_repo, enforcer, notifier


class TestInvestigatorFlow:
    @pytest.mark.asyncio
    async def test_reported_scam_blocks_and_records_provenance(self):
        inv, verdict_repo, enforcer, notifier = _investigator(
            _verdict(Classification.SCAM_HOST, evidence=["cross-origin password form"])
        )
        await inv.investigate(_event())
        enforcer.block_host.assert_awaited_once()
        notifier.safety_action.assert_awaited_once()
        kwargs = verdict_repo.upsert_verdict.await_args.kwargs
        assert kwargs["tier"] == VerdictTier.TOXIC
        assert kwargs["source"] == "llm"
        prov = kwargs["provenance"]
        assert prov["classification"] == "scam_host"
        assert prov["corroborated"] is True
        assert prov["prompt_version"] == "v1+abcd1234"

    @pytest.mark.asyncio
    async def test_burst_scam_without_hard_source_reviews_not_blocks(self):
        inv, _, enforcer, notifier = _investigator(
            _verdict(Classification.SCAM_HOST, evidence=["looks phishy"]),
        )
        await inv.investigate(_event(trigger="pattern", screening=False))
        enforcer.block_host.assert_not_awaited()
        notifier.safety_review.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_burst_scam_self_corroborated_by_feed_hit_blocks(self):
        """A feed hit feed_lookup actually returned corroborates on its own."""
        from services.safety import tools as safety_tools

        async def run_and_set_flag(_task, _bundle):
            # Mutate the shared flag from a CHILD task, exactly as the agent
            # dispatches tools: a rebinding set() here would not survive.
            async def tool_call():
                safety_tools._hard_hit.get().hit = True

            await asyncio.gather(tool_call())
            return _verdict(Classification.SCAM_HOST)

        inv, _, enforcer, _ = _investigator(_verdict(Classification.SCAM_HOST))
        inv._runner.run = AsyncMock(side_effect=run_and_set_flag)
        await inv.investigate(_event(trigger="pattern", screening=False))
        enforcer.block_host.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_model_prose_never_corroborates(self):
        """A model quoting hard-source names must not corroborate anything."""
        inv, _, enforcer, notifier = _investigator(
            _verdict(
                Classification.SCAM_HOST,
                evidence=["HARD HITS on evil.example: feed:fishfish, web_risk"],
            ),
        )
        await inv.investigate(_event(trigger="pattern", screening=False))
        enforcer.block_host.assert_not_awaited()
        notifier.safety_review.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_low_confidence_toxic_never_auto_blocks(self):
        """Corroboration is necessary, not sufficient: high confidence too."""
        inv, verdict_repo, enforcer, _ = _investigator(
            _verdict(Classification.SCAM_HOST, conf=Confidence.LOW)
        )
        await inv.investigate(_event())
        enforcer.block_host.assert_not_awaited()
        assert (
            verdict_repo.upsert_verdict.await_args.kwargs["tier"]
            == VerdictTier.UNCERTAIN
        )

    @pytest.mark.asyncio
    async def test_compromised_legit_blocks_only_reported_aliases(self):
        inv, _, enforcer, _ = _investigator(
            _verdict(Classification.COMPROMISED_LEGIT, scope=Scope.LINKS)
        )
        await inv.investigate(_event(reported_codes=["spoo.me/abc", "custom.com/xyz"]))
        enforcer.block_host.assert_not_awaited()
        pairs = enforcer.block_aliases.await_args.args[0]
        assert ("abc", "spoo.me") in pairs and ("xyz", "custom.com") in pairs

    @pytest.mark.asyncio
    async def test_model_failure_degrades_to_uncertain_and_review(self):
        inv, verdict_repo, enforcer, notifier = _investigator(
            LlmTaskFailed("safety-investigate", "timeout")
        )
        await inv.investigate(_event())
        enforcer.block_host.assert_not_awaited()
        assert verdict_repo.upsert_verdict.await_args.kwargs["tier"] == (
            VerdictTier.UNCERTAIN
        )
        notifier.safety_review.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_failed_sweep_investigation_stays_silent(self):
        inv, _, _, notifier = _investigator(
            LlmTaskFailed("safety-investigate", "model_error")
        )
        await inv.investigate(_event(trigger="sweep"))
        notifier.safety_review.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_redirector_proposal_goes_to_review(self):
        from services.safety.investigation import ListProposal

        inv, _, enforcer, notifier = _investigator(
            _verdict(
                Classification.REDIRECTOR_SERVICE,
                proposals=[
                    ListProposal(list="shorteners", domain="sus.link", why="cloak")
                ],
            )
        )
        await inv.investigate(_event())
        enforcer.block_host.assert_not_awaited()
        review_ctx = notifier.safety_review.await_args.kwargs["context"]
        assert review_ctx["needs"] == "list proposal"
        assert review_ctx["proposals"][0]["domain"] == "sus.link"


class TestScopeAuthority:
    """A host block switches off every link to a host AND refuses all
    future ones. These pin that a narrower scope is always honoured."""

    def test_scam_host_with_path_pattern_scope_blocks_links_not_host(self):
        v = _verdict(Classification.SCAM_HOST, scope=Scope.PATH_PATTERN)
        v = v.model_copy(
            update={"path_pattern": r"^https://sites\.google\.com/view/evil/.*"}
        )
        d = decide_authority(v, corroborated=True, policy=AutoBlockPolicy.CORROBORATED)
        # Unambiguous scam, fully corroborated — and still not host-wide,
        # because the model said the abuse is one path on a shared platform.
        assert d.action == "block_aliases" and d.auto

    def test_scam_host_with_links_scope_blocks_links_not_host(self):
        d = decide_authority(
            _verdict(Classification.SCAM_HOST, scope=Scope.LINKS),
            corroborated=True,
            policy=AutoBlockPolicy.CORROBORATED,
        )
        assert d.action == "block_aliases"

    def test_host_scope_still_blocks_the_host(self):
        d = decide_authority(
            _verdict(Classification.SCAM_HOST, scope=Scope.HOST),
            corroborated=True,
            policy=AutoBlockPolicy.CORROBORATED,
        )
        assert d.action == "block_host"

    @pytest.mark.asyncio
    async def test_review_pending_toxic_claim_is_stored_unenforceable(self):
        """A review-pending toxic claim is stored UNCERTAIN, unenforceable."""
        v = _verdict(Classification.SCAM_HOST, conf=Confidence.MEDIUM)
        inv, verdict_repo, enforcer, _n = _investigator(v, policy=AutoBlockPolicy.BOTH)
        await inv.investigate(_event())

        enforcer.block_host.assert_not_awaited()
        kwargs = verdict_repo.upsert_verdict.await_args.kwargs
        assert kwargs["tier"] == VerdictTier.UNCERTAIN
        assert kwargs["provenance"]["classification"] == "scam_host"

    @pytest.mark.asyncio
    async def test_alias_scoped_enactment_never_stores_a_host_scope(self):
        """The store says what was enacted (alias scope), not what was claimed."""
        v = _verdict(Classification.COMPROMISED_LEGIT)
        inv, verdict_repo, _e, _n = _investigator(v)
        await inv.investigate(_event())

        prov = verdict_repo.upsert_verdict.await_args.kwargs["provenance"]
        assert prov["scope"] == "links"

    def test_narrow_scope_never_widens_an_uncorroborated_verdict(self):
        d = decide_authority(
            _verdict(Classification.SCAM_HOST, scope=Scope.PATH_PATTERN),
            corroborated=False,
            policy=AutoBlockPolicy.CORROBORATED,
        )
        assert d.action == "review"

    @pytest.mark.asyncio
    async def test_pattern_reaches_the_operator_and_the_verdict(self):
        v = _verdict(Classification.SCAM_HOST, scope=Scope.PATH_PATTERN).model_copy(
            update={
                "path_pattern": r"^https://sites\.google\.com/view/evil/.*",
                "scope_justification": "shared site builder, 210 creators",
            }
        )
        inv, verdict_repo, enforcer, notifier = _investigator(v)
        await inv.investigate(_event())

        enforcer.block_host.assert_not_awaited()
        enforcer.block_aliases.assert_not_awaited()
        enforcer.block_matching.assert_awaited_once()
        matcher = enforcer.block_matching.await_args.kwargs["matcher"]
        assert matcher("https://sites.google.com/view/evil/login")
        assert not matcher("https://sites.google.com/view/school-club/home")
        prov = verdict_repo.upsert_verdict.await_args.kwargs["provenance"]
        assert prov["scope"] == "path_pattern"
        assert "sites" in prov["path_pattern"]
        assert prov["scope_justification"] == "shared site builder, 210 creators"
        # The verdict store refuses future matches itself; nothing here writes
        # to the operator blocklist, so the embed must not say it does.
        kw = notifier.safety_action.await_args.kwargs
        assert kw["scope"].startswith("pattern: ")
        assert kw["scope_kind"] == "pattern"
        assert "blocklist" not in kw["scope"]
        assert (
            "pattern" not in kw["reason"]
        )  # the reason is the model's reason, nothing else


class TestCoverageTriggersStaySilent:
    """A sweep asked the question, so nobody is waiting on the answer. 30 of
    35 review pings on the first day came from sweeps whose page had simply
    died — the least actionable thing an operator can be handed."""

    @pytest.mark.asyncio
    async def test_sweep_uncertain_records_without_pinging(self):
        inv, verdict_repo, enforcer, notifier = _investigator(
            _verdict(Classification.UNCERTAIN, conf=Confidence.LOW)
        )

        await inv.investigate(_event(trigger="sweep", screening=False))

        notifier.safety_review.assert_not_awaited()
        notifier.safety_action.assert_not_awaited()
        enforcer.block_host.assert_not_awaited()
        # The verdict is still the record, so it is never re-investigated.
        verdict_repo.upsert_verdict.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_hot_medium_scam_records_without_pinging(self):
        inv, _repo, enforcer, notifier = _investigator(
            _verdict(Classification.SCAM_HOST, conf=Confidence.MEDIUM)
        )

        await inv.investigate(_event(trigger="hot", screening=False))

        notifier.safety_review.assert_not_awaited()
        enforcer.block_host.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_report_still_pings(self):
        """Someone reported this and is waiting: silence would lose it."""
        inv, _repo, _enforcer, notifier = _investigator(
            _verdict(Classification.UNCERTAIN, conf=Confidence.LOW)
        )

        await inv.investigate(_event(trigger="report", screening=False))

        notifier.safety_review.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_sweep_that_finds_a_blockable_scam_still_acts(self):
        """Silence applies to review asks, never to enforcement."""
        inv, _repo, enforcer, notifier = _investigator(
            _verdict(Classification.SCAM_HOST, conf=Confidence.HIGH)
        )

        await inv.investigate(_event(trigger="sweep"))

        enforcer.block_host.assert_awaited_once()
        notifier.safety_action.assert_awaited_once()


class TestProposalsCarryTheAsk:
    """forms.gle came back redirector_service / high with proposals: [] and
    a justification saying no action was warranted, then pinged an operator
    anyway. A propose ping has to carry something to approve."""

    def test_redirector_without_a_proposal_is_recorded_not_asked(self):
        decision = decide_authority(
            _verdict(Classification.REDIRECTOR_SERVICE),
            corroborated=False,
            policy=AutoBlockPolicy.CORROBORATED,
        )
        assert decision.action == "benign"
        assert decision.auto is True
        assert decision.tier == VerdictTier.GRAY

    def test_redirector_with_a_proposal_still_asks(self):
        from services.safety.investigation import ListProposal

        decision = decide_authority(
            _verdict(
                Classification.REDIRECTOR_SERVICE,
                proposals=[
                    ListProposal(list="shorteners", domain="cuted.xyz", why="wrapper")
                ],
            ),
            corroborated=False,
            policy=AutoBlockPolicy.CORROBORATED,
        )
        assert decision.action == "propose"
        assert decision.auto is False


class TestLinksScopeEnforcesWithoutAReport:
    """Only a report carries reported_codes. Under the confident policy a
    sweep-found scam at links scope called block_aliases with an empty list
    and enforced nothing, while the notification said links were blocked."""

    @pytest.mark.asyncio
    async def test_sweep_scam_at_links_scope_blocks_the_judged_url(self):
        inv, _repo, enforcer, notifier = _investigator(
            _verdict(Classification.SCAM_HOST, scope=Scope.LINKS),
            policy=AutoBlockPolicy.CONFIDENT,
        )
        enforcer.block_matching = AsyncMock(
            return_value=AsyncMock(blocked_count=1, legacy_count=0)
        )
        # A real sweep event carries no reported_codes; only a report does.
        sweep = SafetyAnalyzeEvent(
            url="https://evil.example/login",
            host="evil.example",
            registrable_domain="evil.example",
            trigger="sweep",
            context={"sweep": "recent_screen"},
        )

        await inv.investigate(sweep)

        enforcer.block_aliases.assert_not_awaited()
        enforcer.block_host.assert_not_awaited()
        matcher = enforcer.block_matching.await_args.kwargs["matcher"]
        assert matcher("https://evil.example/login")
        assert matcher("https://evil.example/login?utm=1")
        assert not matcher("https://evil.example/other")
        notifier.safety_action.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_report_still_blocks_its_named_codes(self):
        inv, _repo, enforcer, _notifier = _investigator(
            _verdict(Classification.SCAM_HOST, scope=Scope.LINKS),
            policy=AutoBlockPolicy.CONFIDENT,
        )
        enforcer.block_aliases = AsyncMock(return_value=AsyncMock(blocked_count=1))

        await inv.investigate(_event(trigger="report", screening=False))

        enforcer.block_aliases.assert_awaited_once()
        assert enforcer.block_aliases.await_args.args[0] == [("abc", "spoo.me")]
        enforcer.block_matching.assert_not_awaited()


class TestProposalsCloseTheLoop:
    """waa.ai came back redirector_service / high and went nowhere: the prompt
    would not propose a real shortener, a proposal was only ever rendered
    into Discord, and nothing could apply one. This is the apply half."""

    @staticmethod
    def _proposal(list_name: str, domain: str):
        from services.safety.investigation import ListProposal

        return ListProposal(list=list_name, domain=domain, why="test")

    def test_high_confidence_resolve_only_proposal_applies_itself(self):
        d = decide_authority(
            _verdict(
                Classification.REDIRECTOR_SERVICE,
                proposals=[self._proposal("redirectors", "forms.gle")],
            ),
            corroborated=False,
            policy=AutoBlockPolicy.CORROBORATED,
        )
        assert d.action == "apply_list" and d.auto

    def test_refuse_list_proposal_still_waits_for_a_human(self):
        d = decide_authority(
            _verdict(
                Classification.REDIRECTOR_SERVICE,
                proposals=[self._proposal("shorteners", "waa.ai")],
            ),
            corroborated=True,
            policy=AutoBlockPolicy.CONFIDENT,
        )
        assert d.action == "propose" and not d.auto

    def test_mixed_lists_take_the_cautious_path(self):
        d = decide_authority(
            _verdict(
                Classification.REDIRECTOR_SERVICE,
                proposals=[
                    self._proposal("redirectors", "a.example"),
                    self._proposal("shorteners", "b.example"),
                ],
            ),
            corroborated=False,
            policy=AutoBlockPolicy.CORROBORATED,
        )
        assert d.action == "propose"

    def test_medium_confidence_never_self_applies(self):
        d = decide_authority(
            _verdict(
                Classification.REDIRECTOR_SERVICE,
                conf=Confidence.MEDIUM,
                proposals=[self._proposal("redirectors", "forms.gle")],
            ),
            corroborated=False,
            policy=AutoBlockPolicy.CORROBORATED,
        )
        assert d.action == "propose"

    @pytest.mark.asyncio
    async def test_apply_list_writes_the_feed_and_stays_silent(self):
        feed_repo = AsyncMock()
        feed_repo.add = AsyncMock(return_value=True)
        inv, verdict_repo, _enforcer, notifier = _investigator(
            _verdict(
                Classification.REDIRECTOR_SERVICE,
                proposals=[self._proposal("redirectors", "forms.gle")],
            ),
            feed_repo=feed_repo,
        )

        await inv.investigate(_event(trigger="sweep", screening=False))

        feed_repo.add.assert_awaited_once_with("redirectors", "forms.gle")
        notifier.safety_review.assert_not_awaited()
        notifier.safety_action.assert_not_awaited()
        prov = verdict_repo.upsert_verdict.await_args.kwargs["provenance"]
        assert prov["proposals"] == [
            {"list": "redirectors", "domain": "forms.gle", "why": "test"}
        ]

    @pytest.mark.asyncio
    async def test_shortener_proposal_is_persisted_for_the_operator(self):
        """The Discord embed is not a record. The proposal has to survive on
        the verdict so the ops tool can apply it later."""
        feed_repo = AsyncMock()
        inv, verdict_repo, _enforcer, _notifier = _investigator(
            _verdict(
                Classification.REDIRECTOR_SERVICE,
                proposals=[self._proposal("shorteners", "waa.ai")],
            ),
            feed_repo=feed_repo,
        )

        await inv.investigate(_event(trigger="report", screening=False))

        feed_repo.add.assert_not_awaited()
        prov = verdict_repo.upsert_verdict.await_args.kwargs["provenance"]
        assert prov["proposals"][0]["domain"] == "waa.ai"


class TestApplyListWithoutAFeedRepo:
    @pytest.mark.asyncio
    async def test_bails_quietly_when_no_repo_is_wired(self):
        """The inline (worker-less) runtime has no feed repo. A self-applying
        proposal there must not raise, and must not pretend it applied."""
        from services.safety.investigation import ListProposal

        inv, verdict_repo, _enforcer, notifier = _investigator(
            _verdict(
                Classification.REDIRECTOR_SERVICE,
                proposals=[
                    ListProposal(list="redirectors", domain="forms.gle", why="t")
                ],
            )
        )
        inv._feed_repo = None

        await inv.investigate(_event(trigger="sweep", screening=False))

        verdict_repo.upsert_verdict.assert_awaited_once()
        notifier.safety_review.assert_not_awaited()


class TestReviewDecisions:
    """The two should-decide threads on the PR, settled."""

    def test_off_means_no_autonomous_action_even_for_resolve_only(self):
        from services.safety.investigation import ListProposal

        d = decide_authority(
            _verdict(
                Classification.REDIRECTOR_SERVICE,
                proposals=[
                    ListProposal(list="redirectors", domain="forms.gle", why="t")
                ],
            ),
            corroborated=False,
            policy=AutoBlockPolicy.OFF,
        )
        assert d.action == "propose" and not d.auto

    @pytest.mark.asyncio
    async def test_a_sweep_proposal_still_reaches_the_operator(self):
        """A proposal carries something to approve. Silencing it would make a
        waa.ai-class discovery arriving via sweep invisible forever."""
        from services.safety.investigation import ListProposal

        inv, _repo, _enforcer, notifier = _investigator(
            _verdict(
                Classification.REDIRECTOR_SERVICE,
                proposals=[ListProposal(list="shorteners", domain="waa.ai", why="t")],
            )
        )

        await inv.investigate(_event(trigger="sweep", screening=False))

        notifier.safety_review.assert_awaited_once()
        ctx = notifier.safety_review.await_args.kwargs["context"]
        assert ctx["proposals"][0]["domain"] == "waa.ai"

    def test_a_proposal_for_an_unknown_list_fails_parse(self):
        import pydantic

        from services.safety.investigation import ListProposal

        with pytest.raises(pydantic.ValidationError):
            ListProposal(list="manual", domain="x.example", why="t")


class TestPromptCarriesTheClickFixRule:
    """Seven of eighteen medium-confidence scam calls were fake verification
    gates on young domains. The prompt had no rule letting the model treat
    the gate as the payload, so it obeyed the only high examples it had and
    hedged. This pins the rule so a prompt edit cannot quietly drop it."""

    def test_rule_and_worked_example_are_present(self):
        from services.safety.investigation import _DEFAULT_PROMPT as p

        assert "A fake verification gate is the payload" in p
        assert "ClickFix" in p
        assert "Example 10" in p
        assert "5347567.shop" in p
        # The counter-case stays: a real provider challenge is still missing evidence.
        assert "cdn-cgi/challenge-platform" in p

    def test_rule_requires_a_rendered_non_provider_gate_plus_two_signals(self):
        """One weak property must never be enough. The first draft used 'or'
        and a dead page went high on hostname shape alone."""
        from services.safety.investigation import _DEFAULT_PROMPT as p

        rule = p[p.index("A fake verification gate is the payload") :]
        rule = rule[: rule.index("\n")]
        assert "needs TWO things together" in rule
        assert "you RENDERED the gate" in rule
        assert "NOT a real challenge provider" in rule
        assert "failed to render, or a dead 404, is not a gate" in rule
        assert "at least two of" in rule
        assert "never `scam_host` `high`" in rule
        assert "Name the signals you counted" in rule

    def test_example_10_claims_only_what_its_bundle_observed(self):
        from services.safety.investigation import _DEFAULT_PROMPT as p

        ex = p[p.index("**Example 10") :]
        ex = ex[: ex.index("\n\n") if "\n\n" in ex else None]
        assert "Verdict: `scam_host`, `high`" in ex
        assert "first TLS certificate 2 days old (RDAP unavailable)" in ex
        assert "root returns 'Cannot GET /'" in ex
        assert "plus four of the second-part signals" in ex
        # It must not overclaim coverage it did not check.
        assert "on any path" not in ex


class TestEnactCarriesScopeAndScreenshot:
    @pytest.mark.asyncio
    async def test_host_block_embed_gets_scope_and_the_rendered_screenshot(self):
        """fetch_page runs in a child task, so the screenshot reaches the
        embed through the same mutable-holder contextvar as the hard-hit
        flag. Mutating it from a child task here is exactly how the agent
        dispatches tools."""
        from services.safety import tools as safety_tools

        async def run_and_render(_task, _bundle):
            async def tool_call():
                shots = safety_tools._last_render.get().shots
                shots.append(("https://evil.example/login", b"the-page"))
                shots.append(("https://evil.example/", b"blank-root"))

            await asyncio.gather(tool_call())
            return _verdict(Classification.SCAM_HOST)

        inv, _repo, enforcer, notifier = _investigator(
            _verdict(Classification.SCAM_HOST)
        )
        inv._runner.run = AsyncMock(side_effect=run_and_render)

        await inv.investigate(_event())

        enforcer.block_host.assert_awaited_once()
        kw = notifier.safety_action.await_args.kwargs
        assert kw["scope"] == "host-wide"
        assert kw["scope_kind"] == "host"
        # The judged URL's render, not the root fetched after it.
        assert kw["screenshot"] == b"the-page"

    @pytest.mark.asyncio
    async def test_no_render_means_no_attachment(self):
        inv, _repo, _enforcer, notifier = _investigator(
            _verdict(Classification.SCAM_HOST)
        )

        await inv.investigate(_event())

        assert notifier.safety_action.await_args.kwargs["screenshot"] is None


class TestSiblingLinks:
    """A small host's other links, resolved, go into the bundle: campaign
    shape the model cannot see from one render. A shared platform's links
    belong to strangers, so past the cap nothing is listed."""

    def _event(self):
        return SafetyAnalyzeEvent(
            url="https://ourl.jp/EJTQX",
            host="ourl.jp",
            registrable_domain="ourl.jp",
            trigger="sweep",
            context={},
        )

    def _repo(self, link_count, siblings):
        from unittest.mock import AsyncMock

        repo = AsyncMock()
        repo.destination_history = AsyncMock(
            return_value={
                "link_count": link_count,
                "anon_count": link_count,
                "owned_count": 0,
                "distinct_owners": 0,
                "total_clicks": 3,
                "first_seen": None,
                "edited_count": 0,
            }
        )
        repo.list_recent_by_dest_host = AsyncMock(return_value=siblings)
        return repo

    @pytest.mark.asyncio
    async def test_small_host_lists_every_sibling_and_where_it_ends(self):
        from datetime import datetime, timezone

        from services.safety.investigation import build_evidence_bundle

        t0 = datetime(2026, 9, 3, 10, 41, tzinfo=timezone.utc)
        siblings = [
            {
                "alias": "a",
                "long_url": "https://ourl.jp/EJTQX",
                "anonymous": True,
                "created_at": t0,
            },
            {
                "alias": "b",
                "long_url": "https://ourl.jp/9Bdpv",
                "anonymous": True,
                "created_at": t0,
            },
            {
                "alias": "c",
                "long_url": "https://ourl.jp/dead",
                "anonymous": False,
                "created_at": t0,
            },
        ]
        ends = {
            "https://ourl.jp/EJTQX": "https://aceuimg.pages.dev/",
            "https://ourl.jp/9Bdpv": "https://x.github.io/k5/",
            "https://ourl.jp/dead": None,
        }

        async def resolve(url):
            return ends[url]

        text = await build_evidence_bundle(
            self._event(), self._repo(3, siblings), resolve
        )
        assert "## Sibling links on this host" in text
        assert "3 links, 2 anonymous, 1 from accounts" in text
        assert (
            "- https://ourl.jp/EJTQX → https://aceuimg.pages.dev/  [the link under investigation]"
            in text
        )
        assert "- https://ourl.jp/dead → unreachable" in text
        assert (
            "destination hosts: 1x aceuimg.pages.dev, 1x x.github.io, 1x unreachable"
            in text
        )
        # Section order: siblings sit between history and the trigger.
        assert (
            text.index("## First-party history")
            < text.index("## Sibling links")
            < text.index("## Why this reached you")
        )

    @pytest.mark.asyncio
    async def test_shared_platform_is_not_listed(self):
        from services.safety.investigation import build_evidence_bundle

        repo = self._repo(380, [])
        text = await build_evidence_bundle(self._event(), repo, AsyncMock())
        assert "Sibling links" not in text
        repo.list_recent_by_dest_host.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resolver_blowing_up_does_not_kill_the_bundle(self):
        from datetime import datetime, timezone

        from services.safety.investigation import build_evidence_bundle

        siblings = [
            {
                "alias": "a",
                "long_url": "https://ourl.jp/EJTQX",
                "anonymous": True,
                "created_at": datetime(2026, 9, 3, tzinfo=timezone.utc),
            },
        ]

        async def resolve(url):
            raise RuntimeError("dns down")

        text = await build_evidence_bundle(
            self._event(), self._repo(1, siblings), resolve
        )
        assert "- https://ourl.jp/EJTQX → unreachable" in text
