"""SafetyAnalyzer — provider chain → verdict store → enforcement/notify.

The single orchestration point every trigger funnels through, in whichever
process hosts it (worker consumer or inline sink). Semantics:

- A verdict fresher than ``reverdict_ttl`` short-circuits analysis; an
  existing TOXIC verdict re-runs enforcement idempotently instead (new
  links to an already-judged destination die without re-analysis),
  bounded by the verdict's scope.
- First non-abstaining provider wins. No provider judging means tier
  UNCERTAIN and a human review embed — BENIGN is never inferred from
  absence of evidence.
- Screening enforces exactly as far as its signal reaches. A curated feed
  naming a host is a host-wide call and needs no second opinion; anything
  narrower blocks the links it covers and escalates the host question to
  the deep tier.
- Everything is best-effort: analysis failures log and drop, they never
  propagate into the trigger path (a report must store even when analysis
  is broken).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from app.infrastructure.logging import get_logger
from app.repositories.verdict_repository import VerdictRepository
from app.schemas.enums.safety import VerdictTier
from app.schemas.models.verdict import VerdictDoc
from app.services.safety.admission import AdmissionPolicy
from app.services.safety.enforcer import SafetyEnforcer
from app.services.safety.events import SILENT_TRIGGERS, SafetyAnalyzeEvent
from app.services.safety.notify import SafetyNotifier
from app.services.safety.providers import (
    AnalysisProvider,
    ProviderVerdict,
    SharedCarrierLookup,
    verdict_covers,
    without_query,
)
from app.shared.datetime_utils import as_aware_utc
from app.shared.url_utils import parse_destination
from app.shared.validators import is_valid_pattern, matching_blocked_pattern

if TYPE_CHECKING:
    # sinks.py imports the analyzer (inline rung), so the sink protocol is
    # only imported for typing to avoid the cycle.
    from app.services.safety.sinks import DeepAnalysisSink

log = get_logger(__name__)

# A sweep screening must never swallow a user report inside the re-verdict TTL.
_TRIGGER_AUTHORITY = {
    "sweep": 0,
    "hot": 1,
    "redirect": 1,
    "pattern": 1,
    "edit": 2,
    "report": 2,
}


# A curated feed naming a host IS the host-wide answer, so there is no reach
# question left to bill a model for. FeedDomainProvider names itself this way.
_FEED_SOURCE_PREFIX = "feed_"


class SafetyAnalyzer:
    def __init__(
        self,
        providers: list[AnalysisProvider],
        verdict_repo: VerdictRepository,
        enforcer: SafetyEnforcer,
        notifier: SafetyNotifier,
        *,
        reverdict_ttl_hours: int = 24,
        admission: AdmissionPolicy | None = None,
        deep_sink: DeepAnalysisSink | None = None,
        carriers: SharedCarrierLookup | None = None,
    ) -> None:
        self._providers = providers
        self._verdict_repo = verdict_repo
        self._enforcer = enforcer
        self._notifier = notifier
        self._reverdict_ttl = timedelta(hours=reverdict_ttl_hours)
        self._admission = admission
        self._deep_sink = deep_sink
        self._carriers = carriers

    async def analyze(self, event: SafetyAnalyzeEvent) -> None:
        if event.trigger == "redirect" and not (event.context or {}).get("terminal"):
            await self._screen_redirect(event)
            return
        existing = await self._verdict_repo.find_by_host(event.host)
        if existing is not None:
            if existing.tier == VerdictTier.TOXIC and await self._reenforce(
                event, existing
            ):
                return
            # A narrow verdict not covering this URL falls through for a fresh look.
            if existing.decided_by != "system":
                # A human's call never goes stale — this is the allowlist.
                log.info(
                    "safety_analysis_skipped",
                    host=event.host,
                    reason="human_verdict",
                    tier=existing.tier.value,
                )
                return
            updated = as_aware_utc(existing.updated_at)
            incoming = _TRIGGER_AUTHORITY.get(event.trigger, 0)
            stored = _TRIGGER_AUTHORITY.get(existing.trigger or "", 2)
            if (
                updated is not None
                and datetime.now(timezone.utc) - updated < self._reverdict_ttl
                and incoming <= stored
            ):
                log.info(
                    "safety_analysis_skipped",
                    host=event.host,
                    reason="fresh_verdict",
                    tier=existing.tier.value,
                )
                return

        verdict = None
        source = "local_feeds"
        for provider in self._providers:
            verdict = await provider.analyze(
                event.url, event.host, event.registrable_domain
            )
            if verdict is not None:
                source = provider.name
                break

        if verdict is not None and verdict.tier == VerdictTier.TOXIC:
            await self._handle_toxic(event, verdict, source)
            return

        # No judging source (or a non-toxic provider verdict, none of which
        # exist in v1): record UNCERTAIN and hand it to a human with the
        # trigger context attached.
        await self._verdict_repo.upsert_verdict(
            event.host,
            registrable_domain=event.registrable_domain,
            tier=VerdictTier.UNCERTAIN,
            reason=verdict.reason if verdict else None,
            source=source if verdict else "none",
            trigger=event.trigger,
            sample_url=event.url,
            context=event.context,
        )
        # Screening ended unresolved — the admission policy decides who
        # crosses into investigation (the deep tier's own queue). Admitted
        # events skip the immediate review embed: the investigation will
        # come back with a richer result, and two pings for one host is
        # exactly the spam the two-stage split exists to prevent.
        if await self._admit_deep(event):
            return
        if event.trigger in SILENT_TRIGGERS:
            # Coverage screening: the uncertain verdict IS the record.
            log.info("safety_screened", host=event.host, trigger=event.trigger)
            return
        await self._notifier.safety_review(
            host=event.host,
            trigger=event.trigger,
            sample_url=event.url,
            context=event.context,
        )

    async def _handle_toxic(
        self, event: SafetyAnalyzeEvent, verdict: ProviderVerdict, source: str
    ) -> None:
        """Enforce exactly as far as the SIGNAL reaches, never wider and
        never narrower: a feed listing a host is a host-level hard call, a
        blocklist regex covers its pattern, a URL lookup covers that URL."""
        scope, path_pattern = verdict.scope, None
        if scope == "path_pattern":
            if is_valid_pattern(verdict.path_pattern or ""):
                path_pattern = verdict.path_pattern
            else:
                log.warning(
                    "safety_pattern_unusable",
                    host=event.host,
                    pattern=verdict.path_pattern,
                )
                scope = "links"

        if scope == "host":
            result = await self._enforcer.block_host(event.host, reason=verdict.reason)
            scope_note = "host-wide"
        elif scope == "path_pattern":
            pattern = path_pattern or ""
            result = await self._enforcer.block_matching(
                event.host,
                matcher=lambda u: matching_blocked_pattern(u, (pattern,)) is not None,
                reason=verdict.reason,
            )
            scope_note = f"pattern: {pattern}"
        else:
            result = await self._enforcer.block_matching(
                event.host,
                matcher=lambda u, _u=event.url: u == _u,
                reason=verdict.reason,
            )
            scope_note = "the judged link only"

        await self._verdict_repo.upsert_verdict(
            event.host,
            registrable_domain=event.registrable_domain,
            tier=VerdictTier.TOXIC,
            reason=verdict.reason,
            source=source,
            trigger=event.trigger,
            sample_url=event.url,
            context=event.context,
            scope=scope,
            path_pattern=path_pattern,
        )
        if scope == "host" and source.startswith(_FEED_SOURCE_PREFIX):
            await self._warn_if_shared_carrier(event, source)
            follow_up = "feed listing is itself the host-wide answer"
        elif await self._escalate(event, verdict, source):
            follow_up = "host-wide decision sent to investigation"
        else:
            follow_up = "host-wide decision needs review"
        await self._notifier.safety_action(
            host=event.host,
            reason=verdict.reason,
            trigger=event.trigger,
            blocked_count=result.blocked_count,
            legacy_count=result.legacy_count,
            sample_url=event.url,
            scope=scope_note,
            scope_kind={"host": "host", "path_pattern": "pattern", "links": "links"}[
                scope
            ],
            follow_up=follow_up,
        )

    async def _warn_if_shared_carrier(
        self, event: SafetyAnalyzeEvent, source: str
    ) -> None:
        """A feed listing a shortener or share wrapper reaches every
        unrelated link routed through it. Enforcement still follows the
        feed; this is the signal that it was wider than the evidence."""
        if self._carriers is None:
            return
        if await self._carriers.covers(event.host, event.registrable_domain):
            log.warning(
                "safety_feed_block_on_shared_carrier",
                host=event.host,
                registrable_domain=event.registrable_domain,
                source=source,
                # Query strings on these carry recipient ids and tokens.
                sample_url=without_query(event.url),
            )

    async def _reenforce(self, event: SafetyAnalyzeEvent, existing: VerdictDoc) -> bool:
        """Idempotent re-enforcement bounded by the verdict's scope; True when
        the verdict covers this event's URL (nothing left to analyze)."""
        reason = existing.reason or "previous toxic verdict"
        scope = existing.scope or "host"
        if scope == "host":
            # Idempotent, and covers links created after the original block.
            result = await self._enforcer.block_host(event.host, reason=reason)
            await self._notify_reenforced(event, result, reason)
            return True
        if scope == "path_pattern" and existing.path_pattern:
            pattern = existing.path_pattern
            result = await self._enforcer.block_matching(
                event.host,
                matcher=lambda u: matching_blocked_pattern(u, (pattern,)) is not None,
                reason=reason,
            )
            await self._notify_reenforced(
                event, result, reason, scope=f"pattern: {pattern}", scope_kind="pattern"
            )
            return matching_blocked_pattern(event.url, (pattern,)) is not None
        # links scope: already blocked and the create gate refuses the exact URL.
        return event.url == existing.sample_url

    async def _screen_redirect(self, event: SafetyAnalyzeEvent) -> None:
        """Judge where the redirect chain LANDS; the wrapper host itself is
        never judged and never gets a verdict."""
        from app.services.safety.resolver import resolve_terminal_url

        terminal = await resolve_terminal_url(event.url)
        if terminal is None:
            # Unresolved is not clean; it is also not evidence.
            log.info("safety_redirect_unresolved", host=event.host)
            return
        parts = parse_destination(terminal)
        if parts is None or parts["host"] == event.host:
            log.info("safety_redirect_screened", host=event.host)
            return
        await self.analyze(
            SafetyAnalyzeEvent(
                url=terminal,
                host=parts["host"],
                registrable_domain=parts["registrable_domain"],
                trigger="redirect",
                context={
                    **(event.context or {}),
                    "terminal": True,
                    "via": event.url,
                },
            )
        )
        # Wrapped links point at the wrapper; terminal enforcement can't see them.
        verdict = await self._verdict_repo.find_by_host(parts["host"])
        if (
            verdict is not None
            and verdict.tier == VerdictTier.TOXIC
            and verdict_covers(verdict, terminal)
        ):
            reason = verdict.reason or "redirect chain ends at a blocked host"
            result = await self._enforcer.block_matching(
                event.host,
                matcher=lambda u: u == event.url,
                reason=f"{reason} (via {event.host})",
            )
            await self._notifier.safety_action(
                host=parts["host"],
                reason=reason,
                trigger=event.trigger,
                blocked_count=result.blocked_count,
                legacy_count=result.legacy_count,
                sample_url=event.url,
                scope=f"the judged link only (reached through {event.host})",
                scope_kind="links",
            )

    async def _notify_reenforced(
        self,
        event,
        result,
        reason: str,
        scope: str = "host-wide",
        scope_kind: str = "host",
    ) -> None:
        """Blocked something: the operator hears about it. Zero blocks stays quiet."""
        if result.blocked_count + result.legacy_count == 0:
            return
        await self._notifier.safety_action(
            host=event.host,
            reason=reason,
            trigger=event.trigger,
            blocked_count=result.blocked_count,
            legacy_count=result.legacy_count,
            sample_url=event.url,
            scope=f"{scope} (re-enforced existing verdict)",
            scope_kind=scope_kind,
        )

    async def _escalate(
        self, event: SafetyAnalyzeEvent, verdict: ProviderVerdict, source: str
    ) -> bool:
        """Hand the host-wide question to the deep tier; the screening finding
        rides along as the corroborating hard signal."""
        if self._admission is None or self._deep_sink is None:
            return False
        decision = await self._admission.decide(event, escalation=True)
        if not decision.admitted:
            log.info(
                "safety_deep_denied",
                host=event.host,
                trigger=event.trigger,
                reason=decision.reason,
            )
            return False
        await self._deep_sink.emit(
            SafetyAnalyzeEvent(
                url=event.url,
                host=event.host,
                registrable_domain=event.registrable_domain,
                trigger=event.trigger,
                context={
                    **(event.context or {}),
                    "screening": f"{source}: {verdict.reason}",
                },
            )
        )
        log.info(
            "safety_deep_admitted",
            host=event.host,
            trigger=event.trigger,
            reason=decision.reason,
        )
        return True

    async def _admit_deep(self, event: SafetyAnalyzeEvent) -> bool:
        if self._admission is None or self._deep_sink is None:
            return False
        decision = await self._admission.decide(event)
        if decision.admitted:
            await self._deep_sink.emit(event)
            log.info(
                "safety_deep_admitted",
                host=event.host,
                trigger=event.trigger,
                reason=decision.reason,
            )
            return True
        log.info(
            "safety_deep_denied",
            host=event.host,
            trigger=event.trigger,
            reason=decision.reason,
        )
        return False
