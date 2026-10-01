"""URL safety pipeline — the report vertical slice.

Flow: a trigger (reports today; hot links, creation patterns and sweeps
later) emits a ``safety.analyze`` event → the analyzer runs a provider
chain over the destination → the verdict lands in ``safety_verdicts`` →
toxic verdicts are enforced immediately (status flip + cache and edge
eviction + link.blocked events) and everything unjudged goes to a human
review embed. Detection computes once per destination host; the redirect
path only ever reads stored state.
"""

from app.infrastructure.browser_run import BrowserRunClient
from app.services.safety.admission import AdmissionDecision, AdmissionPolicy
from app.services.safety.analyzer import SafetyAnalyzer
from app.services.safety.deep_consumer import DeepAnalysisConsumer
from app.services.safety.enforcer import EnforcementResult, SafetyEnforcer
from app.services.safety.events import SAFETY_STREAM, SafetyAnalyzeEvent
from app.services.safety.feeds import (
    CARRIER_FEEDS,
    FISHFISH_FEED,
    MANUAL_FEED,
    REDIRECTOR_FEED,
    SHORTENER_FEED,
    FishFishClient,
    build_feed_providers,
    build_feed_tasks,
    ensure_feed_seeds,
    fishfish_sync_task,
    load_shortener_seed,
)
from app.services.safety.hot import HotLinkScreen
from app.services.safety.investigation import (
    AutoBlockPolicy,
    DeepInvestigator,
    InvestigationVerdict,
    build_investigate_task,
    decide_authority,
)
from app.services.safety.notify import SafetyNotifier
from app.services.safety.policy import PolicyRejection, UrlPolicyService
from app.services.safety.providers import (
    AnalysisProvider,
    BlockedPatternProvider,
    FeedDomainProvider,
    ProviderVerdict,
    SharedCarrierLookup,
    ToxicVerdictProvider,
    WebRiskProvider,
)
from app.services.safety.resolver import resolve_terminal_url
from app.services.safety.scoring import CreationPatternScorer
from app.services.safety.sinks import (
    DeepAnalysisSink,
    InlineSafetySink,
    NullDeepAnalysisSink,
    NullSafetySink,
    RedisStreamDeepAnalysisSink,
    RedisStreamSafetySink,
    SafetySink,
)
from app.services.safety.sweeps import FeedDeltaSweeper, SweepDeps, build_sweep_tasks
from app.services.safety.tools import (
    InvestigationToolDeps,
    build_investigation_tools,
)

__all__ = [
    "CARRIER_FEEDS",
    "FISHFISH_FEED",
    "MANUAL_FEED",
    "REDIRECTOR_FEED",
    "SAFETY_STREAM",
    "SHORTENER_FEED",
    "AdmissionDecision",
    "AdmissionPolicy",
    "AnalysisProvider",
    "AutoBlockPolicy",
    "BlockedPatternProvider",
    "BrowserRunClient",
    "CreationPatternScorer",
    "DeepAnalysisConsumer",
    "DeepAnalysisSink",
    "DeepInvestigator",
    "EnforcementResult",
    "FeedDeltaSweeper",
    "FeedDomainProvider",
    "FishFishClient",
    "HotLinkScreen",
    "InlineSafetySink",
    "InvestigationToolDeps",
    "InvestigationVerdict",
    "NullDeepAnalysisSink",
    "NullSafetySink",
    "PolicyRejection",
    "ProviderVerdict",
    "RedisStreamDeepAnalysisSink",
    "RedisStreamSafetySink",
    "SafetyAnalyzeEvent",
    "SafetyAnalyzer",
    "SafetyEnforcer",
    "SafetyNotifier",
    "SafetySink",
    "SharedCarrierLookup",
    "SweepDeps",
    "ToxicVerdictProvider",
    "UrlPolicyService",
    "WebRiskProvider",
    "build_feed_providers",
    "build_feed_tasks",
    "build_investigate_task",
    "build_investigation_tools",
    "build_sweep_tasks",
    "decide_authority",
    "ensure_feed_seeds",
    "fishfish_sync_task",
    "load_shortener_seed",
    "resolve_terminal_url",
]
