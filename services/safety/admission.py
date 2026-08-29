"""Admission policy — the one readable rule between screening and
investigation.

Screening (the cheap provider chain) runs for every trigger on one shared
queue. Investigation (the deep tier: outbound calls, renders, the model)
has its own queue and consumer, and is entered ONLY through this policy
when screening ends unresolved. The asymmetry between a report and a
sweep lives here, in one function, instead of as hidden pipeline
behavior:

- ``report``             — admitted within its OWN, larger daily budget.
  Reports are the P0 lane, but they are also the only trigger an outsider
  can pull (anonymous, 40 submissions/day/IP, wildcard DNS for unlimited
  fresh hosts), so priority cannot mean unbudgeted: without a ceiling one
  IP owns the investigation bill.
- ``edit``               — always admitted. A destination edited after
  creation is the bait-and-switch shape; the trigger only exists for
  authenticated links.
- ``pattern`` / ``hot`` / ``redirect`` — admitted within the shared
  daily budget. Bursts, activation spikes and resolved redirect
  endpoints are anomalies worth spending on, but a counter, not a
  blank check.
- ``sweep``              — never admitted by default. Sweep novelty keeps
  its uncertain verdict; coverage is screening's job, not the deep
  tier's. ``SAFETY_DEEP_ADMIT_SWEEPS`` opts sweeps into the same budget.

The budget is a fixed-window counter in the durable queue Redis (the
cache Redis would evict it). Redis being down fails CLOSED for
budget-bound triggers (reports included) — investigation is the expensive
tier, and "we couldn't count, so spend freely" is the wrong failure mode.
A denied report still reaches the human review embed, so nothing is lost
silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from infrastructure.logging import get_logger
from services.safety.events import SafetyAnalyzeEvent

log = get_logger(__name__)

_ALWAYS_ADMITTED = frozenset({"edit"})
_BUDGETED = frozenset({"pattern", "hot", "redirect"})
_BUDGET_KEY_PREFIX = "safety:deep:budget:"
# Two days: the window key outlives its day so a process straddling
# midnight never resurrects an expired counter, then Redis reaps it.
_BUDGET_KEY_TTL_SECONDS = 2 * 24 * 3600


@dataclass(frozen=True)
class AdmissionDecision:
    admitted: bool
    reason: str  # "always" | "within_budget" | "budget_exhausted" |
    #              "sweep_excluded" | "budget_unavailable" | "unknown_trigger"


class AdmissionPolicy:
    def __init__(
        self,
        redis_client,
        *,
        daily_budget: int,
        report_daily_budget: int = 200,
        admit_sweeps: bool = False,
    ) -> None:
        self._redis = redis_client
        self._daily_budget = daily_budget
        self._report_budget = report_daily_budget
        self._admit_sweeps = admit_sweeps

    async def decide(
        self, event: SafetyAnalyzeEvent, *, escalation: bool = False
    ) -> AdmissionDecision:
        """*escalation* marks a toxic screening hit asking for the host-wide
        decision; a sweep escalating a real hit competes for the budget
        instead of being excluded."""
        trigger = event.trigger
        if trigger in _ALWAYS_ADMITTED:
            return AdmissionDecision(True, "always")
        if trigger == "report":
            return await self._within_budget(pool="report", budget=self._report_budget)
        if trigger == "sweep" and not self._admit_sweeps and not escalation:
            return AdmissionDecision(False, "sweep_excluded")
        if trigger in _BUDGETED or trigger == "sweep":
            return await self._within_budget(pool="shared", budget=self._daily_budget)
        # A trigger this policy has never heard of is a coding error
        # upstream; refuse rather than spend on it silently.
        log.warning("safety_deep_unknown_trigger", trigger=trigger)
        return AdmissionDecision(False, "unknown_trigger")

    async def _within_budget(self, *, pool: str, budget: int) -> AdmissionDecision:
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        key = f"{_BUDGET_KEY_PREFIX}{pool}:{day}"
        try:
            used = await self._redis.incr(key)
            if used == 1:
                await self._redis.expire(key, _BUDGET_KEY_TTL_SECONDS)
        except Exception as exc:
            log.warning(
                "safety_deep_budget_unavailable",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return AdmissionDecision(False, "budget_unavailable")
        if used > budget:
            return AdmissionDecision(False, "budget_exhausted")
        return AdmissionDecision(True, "within_budget")
