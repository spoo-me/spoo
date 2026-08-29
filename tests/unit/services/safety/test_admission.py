"""Unit tests for the deep-tier admission policy — the one readable rule
between screening and investigation."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from services.safety.admission import AdmissionPolicy
from services.safety.events import SafetyAnalyzeEvent


def _event(trigger: str) -> SafetyAnalyzeEvent:
    return SafetyAnalyzeEvent(
        url="https://evil.example/kit",
        host="evil.example",
        registrable_domain="evil.example",
        trigger=trigger,
    )


def _redis(used: int = 1) -> AsyncMock:
    r = AsyncMock()
    r.incr = AsyncMock(return_value=used)
    r.expire = AsyncMock()
    return r


class TestAlwaysAdmitted:
    @pytest.mark.asyncio
    async def test_edits_never_consult_the_budget(self):
        redis = _redis()
        policy = AdmissionPolicy(redis, daily_budget=1)
        decision = await policy.decide(_event("edit"))
        assert decision.admitted and decision.reason == "always"
        redis.incr.assert_not_awaited()


class TestReportBudget:
    """Reports get their own, larger budget and never compete with patterns."""

    @pytest.mark.asyncio
    async def test_report_admitted_within_its_own_pool(self):
        redis = _redis(used=1)
        policy = AdmissionPolicy(redis, daily_budget=1, report_daily_budget=200)
        decision = await policy.decide(_event("report"))
        assert decision.admitted and decision.reason == "within_budget"
        key = redis.incr.await_args.args[0]
        assert ":report:" in key

    @pytest.mark.asyncio
    async def test_report_flood_hits_its_ceiling(self):
        redis = _redis(used=201)
        policy = AdmissionPolicy(redis, daily_budget=50, report_daily_budget=200)
        decision = await policy.decide(_event("report"))
        assert not decision.admitted and decision.reason == "budget_exhausted"

    @pytest.mark.asyncio
    async def test_report_budget_fails_closed_when_redis_is_down(self):
        """We couldn't count, so we don't spend."""
        redis = AsyncMock()
        redis.incr = AsyncMock(side_effect=ConnectionError("down"))
        policy = AdmissionPolicy(redis, daily_budget=1)
        decision = await policy.decide(_event("report"))
        assert not decision.admitted and decision.reason == "budget_unavailable"


class TestBudgetedTriggers:
    @pytest.mark.asyncio
    async def test_pattern_within_budget_admits(self):
        policy = AdmissionPolicy(_redis(used=5), daily_budget=10)
        decision = await policy.decide(_event("pattern"))
        assert decision.admitted and decision.reason == "within_budget"

    @pytest.mark.asyncio
    async def test_pattern_over_budget_denies(self):
        policy = AdmissionPolicy(_redis(used=11), daily_budget=10)
        decision = await policy.decide(_event("pattern"))
        assert not decision.admitted and decision.reason == "budget_exhausted"

    @pytest.mark.asyncio
    async def test_first_use_of_the_day_sets_the_window_ttl(self):
        redis = _redis(used=1)
        await AdmissionPolicy(redis, daily_budget=10).decide(_event("pattern"))
        redis.expire.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_budget_unavailable_fails_closed(self):
        """Investigation is the expensive tier: "we couldn't count, so
        spend freely" is the wrong failure mode."""
        redis = AsyncMock()
        redis.incr = AsyncMock(side_effect=ConnectionError("down"))
        policy = AdmissionPolicy(redis, daily_budget=10)
        decision = await policy.decide(_event("pattern"))
        assert not decision.admitted and decision.reason == "budget_unavailable"


class TestSweeps:
    @pytest.mark.asyncio
    async def test_sweeps_excluded_by_default(self):
        redis = _redis()
        policy = AdmissionPolicy(redis, daily_budget=10)
        decision = await policy.decide(_event("sweep"))
        assert not decision.admitted and decision.reason == "sweep_excluded"
        redis.incr.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_opted_in_sweeps_are_budget_bound(self):
        policy = AdmissionPolicy(_redis(used=11), daily_budget=10, admit_sweeps=True)
        decision = await policy.decide(_event("sweep"))
        assert not decision.admitted and decision.reason == "budget_exhausted"


class TestUnknownTrigger:
    @pytest.mark.asyncio
    async def test_unknown_trigger_is_refused_not_spent_on(self):
        policy = AdmissionPolicy(_redis(), daily_budget=10)
        decision = await policy.decide(_event("mystery"))
        assert not decision.admitted and decision.reason == "unknown_trigger"


class TestEscalation:
    """Escalation lifts the sweep exclusion into the budget; all else unchanged."""

    @pytest.mark.asyncio
    async def test_toxic_sweep_competes_for_the_budget(self):
        policy = AdmissionPolicy(_redis(used=1), daily_budget=5, admit_sweeps=False)
        d = await policy.decide(_event("sweep"), escalation=True)
        assert d.admitted and d.reason == "within_budget"

    @pytest.mark.asyncio
    async def test_toxic_sweep_still_bounded_by_the_budget(self):
        policy = AdmissionPolicy(_redis(used=6), daily_budget=5, admit_sweeps=False)
        d = await policy.decide(_event("sweep"), escalation=True)
        assert not d.admitted and d.reason == "budget_exhausted"

    @pytest.mark.asyncio
    async def test_report_escalation_rides_the_report_pool(self):
        redis = _redis(used=1)
        policy = AdmissionPolicy(redis, daily_budget=5)
        d = await policy.decide(_event("report"), escalation=True)
        assert d.admitted and d.reason == "within_budget"
        assert ":report:" in redis.incr.await_args.args[0]


class TestMachineTriggers:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("trigger", ["hot", "redirect"])
    async def test_hot_and_redirect_ride_the_shared_budget(self, trigger):
        policy = AdmissionPolicy(_redis(used=1), daily_budget=5)
        d = await policy.decide(_event(trigger))
        assert d.admitted and d.reason == "within_budget"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("trigger", ["hot", "redirect"])
    async def test_hot_and_redirect_respect_the_ceiling(self, trigger):
        policy = AdmissionPolicy(_redis(used=6), daily_budget=5)
        d = await policy.decide(_event(trigger))
        assert not d.admitted and d.reason == "budget_exhausted"
