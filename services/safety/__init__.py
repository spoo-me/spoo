"""URL safety pipeline — the report vertical slice.

Flow: a trigger (reports today; hot links, creation patterns and sweeps
later) emits a ``safety.analyze`` event → the analyzer runs a provider
chain over the destination → the verdict lands in ``safety_verdicts`` →
toxic verdicts are enforced immediately (status flip + cache and edge
eviction + link.blocked events) and everything unjudged goes to a human
review embed. Detection computes once per destination host; the redirect
path only ever reads stored state.
"""

from infrastructure.browser_run import BrowserRunClient
from services.safety.admission import AdmissionDecision, AdmissionPolicy
from services.safety.analyzer import SafetyAnalyzer
from services.safety.deep_consumer import DeepAnalysisConsumer
from services.safety.enforcer import EnforcementResult, SafetyEnforcer
from services.safety.events import SAFETY_STREAM, SafetyAnalyzeEvent
from services.safety.feeds import (
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
from services.safety.hot import HotLinkScreen
from services.safety.investigation import (
    AutoBlockPolicy,
    DeepInvestigator,
    InvestigationVerdict,
    build_investigate_task,
    decide_authority,
)
from services.safety.policy import PolicyRejection, UrlPolicyService
from services.safety.providers import (
    AnalysisProvider,
    BlockedPatternProvider,
    FeedDomainProvider,
    ProviderVerdict,
    ToxicVerdictProvider,
    WebRiskProvider,
)
from services.safety.resolver import resolve_terminal_url
from services.safety.scoring import CreationPatternScorer
from services.safety.sinks import (
    DeepAnalysisSink,
    InlineSafetySink,
    NullDeepAnalysisSink,
    NullSafetySink,
    RedisStreamDeepAnalysisSink,
    RedisStreamSafetySink,
    SafetySink,
)
from services.safety.sweeps import FeedDeltaSweeper, SweepDeps, build_sweep_tasks
from services.safety.tools import (
    InvestigationToolDeps,
    build_investigation_tools,
)

__all__ = [
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
    "SafetySink",
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
