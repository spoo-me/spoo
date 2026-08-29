"""L2 investigation — the deep tier's task, authority mapper and consumer.

The model classifies; code decides how far the classification reaches.
That split is the whole safety story of this tier: the model can be
wrong about *what* a destination is, but it can never on its own take an
action wider than its evidence justifies. The authority mapper is a pure
function of (classification, confidence, corroboration) → action, and
corroboration means an INDEPENDENT hard signal (the report trigger, a
feed hit, or Web Risk) agreed — never the model's own confidence
restated.

Auto-block policy is a config value (``SAFETY_DEEP_AUTOBLOCK``):
``corroborated`` (default) blocks only when a hard source agrees and
sends everything else to review; ``confident`` lets a high-confidence
model verdict block alone; ``both`` requires both; ``off`` never
auto-blocks. The policy graduates by measurement — every review tap is a
label — not by trusting the model up front.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

from infrastructure.llm import LlmTask, LlmTaskFailed, LlmTaskRunner, load_prompt
from infrastructure.logging import get_logger
from repositories.url_repository import UrlRepository
from repositories.verdict_repository import VerdictRepository
from schemas.enums.safety import VerdictTier
from services.safety.enforcer import SafetyEnforcer
from services.safety.events import SafetyAnalyzeEvent
from services.safety.tools import reset_hard_hit, saw_hard_hit
from shared.validators import is_valid_pattern, matching_blocked_pattern

log = get_logger(__name__)

INVESTIGATE_TASK = "safety-investigate"
_PROMPT_VERSION = "v1"

_DEFAULT_PROMPT = (Path(__file__).parent / "prompts" / "investigate_v1.md").read_text()


class Classification(str, Enum):
    SCAM_HOST = "scam_host"
    COMPROMISED_LEGIT = "compromised_legit"
    REDIRECTOR_SERVICE = "redirector_service"
    LEGIT_RELAY = "legit_relay"
    SPAM_GRAY = "spam_gray"
    BENIGN = "benign"
    UNCERTAIN = "uncertain"


class Confidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Scope(str, Enum):
    """How far the model believes its finding reaches.

    ``PATH_PATTERN`` exists because host-wide is the wrong answer on a
    shared platform: one abusive Google Sites page or one bad
    githubusercontent user does not make the platform abusive. The model
    proposes the narrowest scope that covers the abuse, and code still
    decides what authority that carries.
    """

    HOST = "host"
    LINKS = "links"
    PATH_PATTERN = "path_pattern"


class ListProposal(BaseModel):
    list: str  # e.g. "shorteners" | "manual"
    domain: str
    why: str


class InvestigationVerdict(BaseModel):
    """The model's claim — never an action. Code owns authority."""

    classification: Classification
    confidence: Confidence
    reason: str = Field(description="one operator-facing sentence")
    evidence: list[str] = Field(default_factory=list)
    scope: Scope = Scope.HOST
    # Required when scope is PATH_PATTERN: the narrowest regex covering the
    # abuse, e.g. ^https://sites\\.google\\.com/view/evil-page/.*  — an
    # operator reviews it before it ever reaches the blocklist.
    path_pattern: str | None = None
    # Why host-wide is or is not justified. Demanded separately from
    # ``reason`` so the scope decision is auditable on its own: a host
    # block is the most destructive action this system takes.
    scope_justification: str | None = Field(
        default=None,
        description="why the chosen scope is right, especially if host-wide",
    )
    proposals: list[ListProposal] = Field(default_factory=list)


# Which classifications are toxic (block-worthy) vs let-live. The model
# picks a category; THIS is where categories become tiers, so retiering a
# class is a one-line code change and never a prompt change.
_TOXIC = {Classification.SCAM_HOST, Classification.COMPROMISED_LEGIT}
_BENIGN = {Classification.BENIGN, Classification.LEGIT_RELAY}


class AutoBlockPolicy(str, Enum):
    CORROBORATED = "corroborated"  # hard source must agree (default)
    CONFIDENT = "confident"  # high-confidence model verdict blocks alone
    BOTH = "both"  # both required
    OFF = "off"  # never auto-block; always review


@dataclass(frozen=True)
class AuthorityDecision:
    """What code will actually do with the model's claim."""

    action: str  # "block_host" | "block_aliases" | "propose" | "benign" | "review"
    tier: VerdictTier
    auto: bool  # did this enforce on its own, or is it a review ask?


def decide_authority(
    verdict: InvestigationVerdict,
    *,
    corroborated: bool,
    policy: AutoBlockPolicy,
) -> AuthorityDecision:
    """Pure function: (claim, corroboration, policy) → action. Blast
    radius is the axis — a self-limiting per-destination verdict can apply
    itself; anything that reaches future links waits for a human."""
    cls = verdict.classification
    high = verdict.confidence == Confidence.HIGH

    if cls in _BENIGN:
        # A benign verdict is self-limiting and safe to store; system
        # benign still re-screens later (the decided_by rule), a human tap
        # makes it permanent.
        return AuthorityDecision("benign", VerdictTier.BENIGN, auto=True)

    if cls == Classification.REDIRECTOR_SERVICE:
        # Adding a whole service to a block list reaches every FUTURE link
        # to it — always a human tap, regardless of confidence.
        return AuthorityDecision("propose", VerdictTier.GRAY, auto=False)

    if cls == Classification.SPAM_GRAY:
        # Gray policy is let-it-live (interstitial tier unbuilt); record,
        # don't block.
        return AuthorityDecision("benign", VerdictTier.GRAY, auto=True)

    if cls in _TOXIC:
        may_block = _may_auto_block(high, corroborated, policy)
        if not may_block:
            return AuthorityDecision("review", VerdictTier.TOXIC, auto=False)
        # compromised_legit NEVER gets a host-wide verdict — the host is a
        # real business; only the specific links die.
        if cls == Classification.COMPROMISED_LEGIT:
            return AuthorityDecision("block_aliases", VerdictTier.TOXIC, auto=True)
        # A narrower scope than host is honoured even on scam_host: the
        # model saw a shared platform. Kill the matching links now; the
        # PATTERN reaches every future link, so it waits for a human.
        if verdict.scope in (Scope.LINKS, Scope.PATH_PATTERN):
            return AuthorityDecision("block_aliases", VerdictTier.TOXIC, auto=True)
        return AuthorityDecision("block_host", VerdictTier.TOXIC, auto=True)

    # UNCERTAIN and anything unmapped → a human looks.
    return AuthorityDecision("review", VerdictTier.UNCERTAIN, auto=False)


def _may_auto_block(high: bool, corroborated: bool, policy: AutoBlockPolicy) -> bool:
    if policy == AutoBlockPolicy.OFF:
        return False
    if policy == AutoBlockPolicy.CORROBORATED:
        # Necessary, not sufficient: a hedged toxic call must not enforce.
        return corroborated and high
    if policy == AutoBlockPolicy.CONFIDENT:
        return high
    return high and corroborated  # BOTH


def build_investigate_task(prompt_dir: str = "", tools=()) -> LlmTask:
    """The single registered LLM task for the deep tier."""
    return LlmTask(
        name=INVESTIGATE_TASK,
        prompt_version=_PROMPT_VERSION,
        system_prompt=load_prompt(INVESTIGATE_TASK, _DEFAULT_PROMPT, prompt_dir),
        output_type=InvestigationVerdict,
        tools=tools,
    )


async def build_evidence_bundle(
    event: SafetyAnalyzeEvent, url_repo: UrlRepository
) -> str:
    """Everything free goes in the prompt: the URL, its decomposition,
    first-party history, the report text, and why it was queued. NEVER our
    own prior system verdicts — that would make the store an echo chamber.
    Human verdicts on siblings would go here too (deferred: needs a
    registrable-scoped human-verdict read)."""
    history = await url_repo.destination_history(event.host)
    ctx = event.context or {}
    lines = [
        "## Destination",
        f"url: {event.url}",
        f"host: {event.host}",
        f"registrable domain: {event.registrable_domain}",
        "",
        "## First-party history (what we already know)",
        f"links pointing here: {history['link_count']} "
        f"({history['anon_count']} anonymous, {history['owned_count']} from accounts)",
        f"distinct account creators: {history['distinct_owners']}",
        f"total clicks across those links: {history['total_clicks']}",
        f"first seen: {history['first_seen'] or 'unknown'}",
        f"links edited after creation: {history['edited_count']}",
        "",
        "## Why this reached you",
        f"trigger: {event.trigger}",
    ]
    if ctx.get("reasons"):
        lines.append(f"report reasons: {', '.join(ctx['reasons'])}")
    if ctx.get("reported_codes"):
        lines.append(f"reported links: {', '.join(ctx['reported_codes'])}")
    if ctx.get("screening"):
        lines.append(f"cheap-tier findings: {ctx['screening']}")
    lines.append("")
    lines.append(
        "Judge this destination. Use tools only where they would change "
        "your answer, then return the structured verdict."
    )
    return "\n".join(lines)


class DeepInvestigator:
    """Consumes the investigation queue: build the bundle, run the task,
    map the claim to authority, write the verdict, enforce or ask a
    human. Every failure degrades to an uncertain verdict + review — the
    deep tier breaking must never lose a reported host."""

    def __init__(
        self,
        runner: LlmTaskRunner,
        task: LlmTask,
        url_repo: UrlRepository,
        verdict_repo: VerdictRepository,
        enforcer: SafetyEnforcer,
        notifier,
        *,
        policy: AutoBlockPolicy,
        model_name: str,
    ) -> None:
        self._runner = runner
        self._task = task
        self._url_repo = url_repo
        self._verdict_repo = verdict_repo
        self._enforcer = enforcer
        self._notifier = notifier
        self._policy = policy
        self._model = model_name

    async def investigate(self, event: SafetyAnalyzeEvent) -> None:
        bundle = await build_evidence_bundle(event, self._url_repo)
        reset_hard_hit()
        try:
            verdict: InvestigationVerdict = await self._runner.run(self._task, bundle)
        except LlmTaskFailed as exc:
            # Model declined, timed out, or hit a ceiling: the host is not
            # judged clean — it keeps its uncertain verdict and a human is
            # asked to look (report/pattern triggers) or it stays on record
            # (sweep). Never a silent pass.
            log.warning(
                "safety_investigation_failed",
                host=event.host,
                reason=exc.reason,
            )
            await self._record_and_review(event, reason=f"investigation {exc.reason}")
            return

        corroborated = self._corroborated(event)
        decision = decide_authority(
            verdict, corroborated=corroborated, policy=self._policy
        )
        # Store what was ENACTED, not claimed: re-enforcement and the create
        # gate read these, and an unapproved claim must not enforce itself.
        stored_tier = decision.tier
        if decision.tier is VerdictTier.TOXIC and not decision.auto:
            stored_tier = VerdictTier.UNCERTAIN
        stored_scope = verdict.scope.value
        stored_pattern = verdict.path_pattern
        if verdict.scope is Scope.PATH_PATTERN and not is_valid_pattern(
            verdict.path_pattern or ""
        ):
            log.warning(
                "safety_model_pattern_invalid",
                host=event.host,
                pattern=verdict.path_pattern,
            )
            stored_scope, stored_pattern = Scope.LINKS.value, None
        if decision.action == "block_host":
            stored_scope = Scope.HOST.value
        elif decision.action == "block_aliases" and verdict.scope is Scope.HOST:
            stored_scope = Scope.LINKS.value
        provenance = {
            "model": self._model,
            "prompt_version": self._task.versioned_prompt,
            "classification": verdict.classification.value,
            "confidence": verdict.confidence.value,
            "evidence": verdict.evidence,
            "egress": None,  # set by fetch_page tool usage; recorded in evidence
            "corroborated": corroborated,
            "scope": stored_scope,
            "path_pattern": stored_pattern,
            "scope_justification": verdict.scope_justification,
        }
        await self._verdict_repo.upsert_verdict(
            event.host,
            registrable_domain=event.registrable_domain,
            tier=stored_tier,
            reason=verdict.reason,
            source="llm",
            trigger=event.trigger,
            sample_url=event.url,
            context=event.context,
            provenance=provenance,
        )
        log.info(
            "safety_investigated",
            host=event.host,
            classification=verdict.classification.value,
            confidence=verdict.confidence.value,
            corroborated=corroborated,
            action=decision.action,
            auto=decision.auto,
        )
        await self._enact(event, verdict, decision)

    def _corroborated(self, event: SafetyAnalyzeEvent) -> bool:
        """An INDEPENDENT hard signal agreed with a toxic call — computed only
        from facts code observed (the screening context or the feed_lookup
        contextvar), never from the model's prose or the trigger alone."""
        screening = ((event.context or {}).get("screening") or "").lower()
        if any(src in screening for src in ("feed", "web_risk", "blocked_pattern")):
            return True
        return saw_hard_hit()

    async def _enact(
        self,
        event: SafetyAnalyzeEvent,
        verdict: InvestigationVerdict,
        decision: AuthorityDecision,
    ) -> None:
        if decision.action == "block_host":
            result = await self._enforcer.block_host(event.host, reason=verdict.reason)
            await self._notifier.safety_action(
                host=event.host,
                reason=verdict.reason,
                trigger=event.trigger,
                blocked_count=result.blocked_count,
                legacy_count=result.legacy_count,
                sample_url=event.url,
            )
        elif decision.action == "block_aliases":
            if (
                verdict.scope == Scope.PATH_PATTERN
                and verdict.path_pattern
                and is_valid_pattern(verdict.path_pattern)
            ):
                # The pattern still waits for a human before it can refuse creates.
                pattern = verdict.path_pattern
                result = await self._enforcer.block_matching(
                    event.host,
                    matcher=lambda u: (
                        matching_blocked_pattern(u, (pattern,)) is not None
                    ),
                    reason=verdict.reason,
                )
                blocked_count, legacy_count = (
                    result.blocked_count,
                    result.legacy_count,
                )
                scope_note = f"pattern proposed for the blocklist: {pattern}"
            else:
                pairs = await self._aliases_to_block(event)
                alias_result = await self._enforcer.block_aliases(
                    pairs, host=event.host, reason=verdict.reason
                )
                blocked_count, legacy_count = alias_result.blocked_count, 0
                scope_note = "specific links only, host left serving"
            await self._notifier.safety_action(
                host=event.host,
                reason=f"{verdict.reason} ({scope_note})",
                trigger=event.trigger,
                blocked_count=blocked_count,
                legacy_count=legacy_count,
                sample_url=event.url,
            )
        elif decision.action in ("review", "propose"):
            await self._notifier.safety_review(
                host=event.host,
                trigger=event.trigger,
                sample_url=event.url,
                context={
                    **(event.context or {}),
                    "classification": verdict.classification.value,
                    "confidence": verdict.confidence.value,
                    "reason": verdict.reason,
                    "proposals": [p.model_dump() for p in verdict.proposals],
                    "scope": verdict.scope.value,
                    "path_pattern": verdict.path_pattern,
                    "scope_justification": verdict.scope_justification,
                    "needs": "list proposal"
                    if decision.action == "propose"
                    else "block decision",
                },
            )
        # "benign": the verdict is the record; nothing else to do.

    async def _aliases_to_block(
        self, event: SafetyAnalyzeEvent
    ) -> list[tuple[str, str]]:
        """For a compromised-legit host we block only the reported links,
        not everything pointing at the host. The reported codes carry the
        (domain/code) shape from the report context."""
        pairs: list[tuple[str, str]] = []
        for entry in (event.context or {}).get("reported_codes", []):
            if "/" in entry:
                domain, code = entry.split("/", 1)
                pairs.append((code, domain))
        return pairs

    async def _record_and_review(
        self, event: SafetyAnalyzeEvent, *, reason: str
    ) -> None:
        await self._verdict_repo.upsert_verdict(
            event.host,
            registrable_domain=event.registrable_domain,
            tier=VerdictTier.UNCERTAIN,
            reason=reason,
            source="llm",
            trigger=event.trigger,
            sample_url=event.url,
            context=event.context,
        )
        if event.trigger == "sweep":
            log.info("safety_investigation_screened", host=event.host)
            return
        await self._notifier.safety_review(
            host=event.host,
            trigger=event.trigger,
            sample_url=event.url,
            context=event.context,
        )
