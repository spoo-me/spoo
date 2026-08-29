"""
Service and repository wiring — the composition root.

Called once during app startup to build all repositories, infrastructure,
and services as singletons on ``app.state``.  This keeps the lifespan
function in app.py focused on infrastructure lifecycle (connect/disconnect)
while this module handles the object graph.
"""

from __future__ import annotations

from fastapi import FastAPI

from config import AppSettings
from infrastructure.cache.feature_flag_cache import FeatureFlagCache
from infrastructure.cache.meta_fetch_cache import MetaFetchCache
from infrastructure.cache.onboarding_cache import OnboardingCache
from infrastructure.cache.url_cache import UrlCache
from infrastructure.captcha.hcaptcha import HCaptchaProvider
from infrastructure.cloudflare_client import CloudflareClient
from infrastructure.cloudflare_kv import CloudflareKVClient
from infrastructure.logging import get_logger
from infrastructure.ops_notify import DiscordOpsNotifier
from infrastructure.storage.r2 import R2StorageClient
from repositories.api_key_repository import ApiKeyRepository
from repositories.app_grant_repository import AppGrantRepository
from repositories.blocked_domain_repository import BlockedDomainRepository
from repositories.blocked_url_repository import BlockedUrlRepository
from repositories.click_repository import ClickRepository
from repositories.custom_domain_repository import CustomDomainRepository
from repositories.feature_flag_repository import FeatureFlagRepository
from repositories.feed_domain_repository import FeedDomainRepository
from repositories.legacy.emoji_url_repository import EmojiUrlRepository
from repositories.legacy.legacy_url_repository import LegacyUrlRepository
from repositories.page_layout_repository import PageLayoutRepository
from repositories.report_repository import (
    ReportRepository,
    ReportSubmissionRepository,
)
from repositories.scheduled_task_repository import ScheduledTaskRepository
from repositories.token_repository import TokenRepository
from repositories.url_repository import UrlRepository
from repositories.user_repository import UserRepository
from repositories.verdict_repository import VerdictRepository
from repositories.webhook_delivery_repository import WebhookDeliveryRepository
from repositories.webhook_endpoint_repository import WebhookEndpointRepository
from repositories.webhook_event_repository import WebhookEventRepository
from schemas.enums.domain_status import VerificationMethod
from services.api_key_service import ApiKeyService
from services.auth.credentials import CredentialService
from services.auth.device import DeviceAuthService
from services.auth.otp import OtpService
from services.auth.password import PasswordService
from services.auth.verification import EmailVerificationService
from services.bulk_url_service import BulkUrlService
from services.cf_saas_backend import CfSaasBackend
from services.click import ClickService, LegacyClickHandler, V2ClickHandler
from services.click.sinks import InlineSink, RedisStreamSink
from services.contact_service import ContactService
from services.custom_domain_service import CustomDomainService
from services.domain_intel_service import DomainIntelService
from services.edge_cache.og_writethrough import OgEdgeWritethrough
from services.events.sinks import (
    InlineDomainEventSink,
    NullDomainEventSink,
    StreamDomainEventSink,
)
from services.export.formatters import default_formatters
from services.export.service import ExportService
from services.feature_flag_service import FeatureFlagService
from services.meta_tags.sinks import NullMetaImageSink, RedisStreamMetaImageSink
from services.mock_dcv_backend import MockDcvBackend
from services.oauth_service import OAuthService
from services.page_layout_service import PageLayoutService
from services.profile_picture_service import ProfilePictureService
from services.public_link_resolver import PublicLinkResolver
from services.public_preview_service import PublicPreviewService
from services.public_stats_service import PublicStatsService
from services.report_intake_service import ReportIntakeService
from services.safety import (
    AdmissionPolicy,
    BlockedPatternProvider,
    CreationPatternScorer,
    DeepAnalysisSink,
    FeedDeltaSweeper,
    InlineSafetySink,
    NullDeepAnalysisSink,
    NullSafetySink,
    RedisStreamDeepAnalysisSink,
    RedisStreamSafetySink,
    SafetyAnalyzer,
    SafetyEnforcer,
    SafetySink,
    SweepDeps,
    ToxicVerdictProvider,
    UrlPolicyService,
    WebRiskProvider,
    build_feed_providers,
    build_feed_tasks,
    build_sweep_tasks,
)
from services.scheduler import TaskScheduler
from services.scheduler.tasks import build_task_registry
from services.stats_service import StatsService
from services.tenant_resolver import CachedMongoTenantResolver
from services.token_factory import TokenFactory
from services.url_expand_service import UrlExpandService
from services.url_service import UrlService
from services.webhooks import (
    DeliveryExecutor,
    OwnerSubscriptionCache,
    SubscriptionMatcher,
    WebhookDispatcher,
    WebhookService,
)
from services.webhooks.consumers import WebhookFanoutClickSink
from services.webhooks.renderers import default_renderers

log = get_logger(__name__)


def build_click_service(
    click_repo: ClickRepository,
    url_repo: UrlRepository,
    legacy_repo: LegacyUrlRepository,
    emoji_repo: EmojiUrlRepository,
    geoip,
    url_cache: UrlCache,
    events=None,
) -> ClickService:
    """Compose the click pipeline (schema handler registry).

    Single source of truth for the schema→handler mapping, shared by the
    web app (inline sink) and the click worker (stats consumer) so both
    processes always run identical tracking logic. ``events`` is the
    domain-event sink for link.expired (max-clicks branch); None = off.
    """
    return ClickService(
        {
            "v2": V2ClickHandler(click_repo, url_repo, geoip, url_cache, events=events),
            "v1": LegacyClickHandler(legacy_repo, emoji_repo, geoip),
        }
    )


def wire_services(app: FastAPI, settings: AppSettings, redis_client) -> None:
    """Build all repositories and services, store on ``app.state``.

    Called once from the lifespan after infrastructure (db, redis, geoip,
    http_client, email_provider) is ready on ``app.state``.
    """
    db = app.state.db
    http_client = app.state.http_client

    # ── Repositories ─────────────────────────────────────────────────────
    url_repo = UrlRepository(db["urlsV2"])
    legacy_repo = LegacyUrlRepository(db["urls"])
    emoji_repo = EmojiUrlRepository(db["emojis"])
    click_repo = ClickRepository(db["clicks"])
    user_repo = UserRepository(db["users"])
    token_repo = TokenRepository(db["verification-tokens"])
    api_key_repo = ApiKeyRepository(db["api-keys"])
    page_layout_repo = PageLayoutRepository(db["page-layouts"])
    blocked_url_repo = BlockedUrlRepository(db["blocked-urls"])
    app_grant_repo = AppGrantRepository(db["app-grants"])
    feature_flag_repo = FeatureFlagRepository(db["feature_flags"])

    # ── Infrastructure ───────────────────────────────────────────────────
    url_cache = UrlCache(redis_client, ttl_seconds=settings.redis.redis_ttl_seconds)
    app.state.meta_fetch_cache = MetaFetchCache(redis_client)
    app.state.onboarding_cache = OnboardingCache(redis_client)
    feature_flag_cache = FeatureFlagCache(
        redis_client,
        ttl_seconds=settings.redis.feature_flag_ttl_seconds,
        negative_ttl_seconds=settings.redis.feature_flag_negative_ttl_seconds,
    )
    captcha = HCaptchaProvider(settings.hcaptcha_secret, http_client)
    # One notifier, two channels — env vars keep their shipped names
    # (CONTACT_WEBHOOK / URL_REPORT_WEBHOOK, they ARE Discord webhook URLs).
    ops_notifier = DiscordOpsNotifier(
        settings.contact_webhook, settings.url_report_webhook, http_client
    )

    # Edge KV client, shared by the og write-through and the bulk ops'
    # edge flush. None when the edge cache isn't configured (self-host) —
    # origin serves all previews and bulk ops skip edge purging entirely.
    og_writethrough = None
    edge_kv_client = None
    edge = settings.edge_cache
    if edge.enabled:
        edge_kv_client = CloudflareKVClient(
            http_client=http_client,
            api_token=edge.cf_api_token,
            account_id=edge.cf_account_id,
            namespace_id=edge.kv_namespace_id,
            api_base=edge.api_base,
            api_host_header=edge.api_host_header,
        )
        # Eager write-through for custom meta-tags: preview bots get
        # answered at the edge from the moment a link's tags are written.
        og_writethrough = OgEdgeWritethrough(
            edge_kv_client,
            system_domain=settings.system_default_domain,
            ttl_seconds=edge.og_ttl_seconds,
        )
        log.info("og_writethrough_enabled", kv_namespace_id=edge.kv_namespace_id)

    # R2 bucket for uploaded og:images. None when unconfigured (self-host):
    # data-URI uploads are rejected with a clear error, https URLs work.
    r2_storage = None
    r2 = settings.r2
    if r2.enabled:
        r2_storage = R2StorageClient(
            http_client=http_client,
            account_id=r2.account_id,
            access_key_id=r2.access_key_id,
            secret_access_key=r2.secret_access_key,
            bucket=r2.bucket,
            public_base_url=r2.public_base_url,
            endpoint_url=r2.endpoint_url,
            request_timeout_seconds=r2.request_timeout_seconds,
        )
        log.info("r2_storage_enabled", bucket=r2.bucket)
    elif any(
        (
            r2.account_id,
            r2.access_key_id,
            r2.secret_access_key,
            r2.bucket,
            r2.public_base_url,
        )
    ):
        log.warning(
            "r2_storage_partial_config",
            detail="some R2_* vars are set but not all five — uploads disabled",
        )

    # Async og:image validation producer — rides the click queue Redis;
    # Null sink (silently skipped) when the queue isn't configured.
    mt = settings.meta_tags
    queue_redis_for_meta = getattr(app.state, "queue_redis", None)
    if mt.async_image_validation and queue_redis_for_meta is not None:
        meta_image_sink = RedisStreamMetaImageSink(
            queue_redis_for_meta, stream=mt.stream, maxlen=mt.maxlen
        )
        log.info("meta_image_validation_enabled", stream=mt.stream)
    else:
        meta_image_sink = NullMetaImageSink()

    # ── Webhooks system ──────────────────────────────────────────────
    # Built BEFORE the services so producers receive the sink at
    # construction. Transport degrades with available infrastructure:
    # queue Redis present → stream sink (the click worker consumes),
    # absent → inline dispatch at emit time. The delivery executor is
    # Mongo-only and runs embedded in this process when the runtime
    # resolves to "embedded" (see app.py lifespan).
    wh_settings = settings.webhooks
    queue_redis_for_webhooks = getattr(app.state, "queue_redis", None)
    webhook_event_repo = WebhookEventRepository(db["webhook-events"])
    webhook_endpoint_repo = WebhookEndpointRepository(db["webhook-endpoints"])
    webhook_delivery_repo = WebhookDeliveryRepository(db["webhook-deliveries"])
    webhook_owner_cache = OwnerSubscriptionCache(
        redis_client, ttl_seconds=wh_settings.matcher_cache_ttl_seconds
    )
    webhook_dispatcher = WebhookDispatcher(
        SubscriptionMatcher(webhook_endpoint_repo, webhook_owner_cache),
        webhook_event_repo,
        webhook_delivery_repo,
        webhook_endpoint_repo,
        max_pending_per_endpoint=wh_settings.max_pending_per_endpoint,
    )
    webhook_executor = DeliveryExecutor(
        webhook_delivery_repo,
        webhook_endpoint_repo,
        webhook_event_repo,
        default_renderers(),
        master_secret=settings.secret_key,
        delivery_timeout=wh_settings.delivery_timeout_seconds,
        max_payload_bytes=wh_settings.max_payload_bytes,
        max_consecutive_failures=wh_settings.max_consecutive_failures,
        poll_interval=wh_settings.executor_poll_seconds,
        lease_seconds=wh_settings.executor_lease_seconds,
    )
    if not wh_settings.enabled:
        app.state.domain_event_sink = NullDomainEventSink()
    elif queue_redis_for_webhooks is not None:
        app.state.domain_event_sink = StreamDomainEventSink(
            queue_redis_for_webhooks,
            fallback=InlineDomainEventSink(webhook_dispatcher),
            maxlen=wh_settings.domain_stream_maxlen,
        )
        log.info("webhooks_stream_sink_enabled")
    else:
        app.state.domain_event_sink = InlineDomainEventSink(webhook_dispatcher)
        log.info("webhooks_inline_sink_enabled")
    # Embedded runtime: the executor lives in this process when webhooks
    # are on and either explicitly requested or (auto) no worker consumes —
    # i.e. the inline rung. app.py starts/cancels the task.
    app.state.webhook_executor = webhook_executor
    app.state.webhook_executor_embedded = wh_settings.enabled and (
        wh_settings.runtime == "embedded"
        or (wh_settings.runtime == "auto" and queue_redis_for_webhooks is None)
    )
    app.state.webhook_service = WebhookService(
        webhook_endpoint_repo,
        webhook_delivery_repo,
        webhook_event_repo,
        webhook_executor,
        webhook_owner_cache,
        master_secret=settings.secret_key,
        max_endpoints=wh_settings.max_endpoints,
    )

    # ── Safety pipeline ──────────────────────────────────────────────
    # Analyzer + enforcer are built unconditionally (cheap objects); the
    # SINK encodes the degradation ladder: disabled → Null, queue Redis →
    # stream (worker analyzes), otherwise inline in this process.
    sf_settings = settings.safety
    safety_enforcer = SafetyEnforcer(
        url_repo,
        legacy_repo,
        emoji_repo,
        UrlCache(redis_client, ttl_seconds=settings.redis.redis_ttl_seconds),
        events=app.state.domain_event_sink,
        edge_kv=edge_kv_client,
        system_default_domain=settings.system_default_domain,
    )
    # Each provider is built ONCE and composed twice: the L0 gate takes
    # the cheap/local subset (blocks the 201, so no network calls), the
    # analyzer takes the full chain. Same instances = same pattern cache,
    # same feed set, one implementation per signal. Feed membership comes
    # from FEED_REGISTRY (catalog as code) — adding a feed never edits
    # this function.
    feed_domain_repo = FeedDomainRepository(db["safety_feed_domains"])
    verdict_repo = VerdictRepository(db["safety_verdicts"])
    pattern_provider = BlockedPatternProvider(
        blocked_url_repo, regex_timeout=settings.blocked_url_regex_timeout
    )
    feed_gate, feed_analyzer, feed_messages = build_feed_providers(
        sf_settings, feed_domain_repo
    )
    gate_providers: list = [
        pattern_provider,
        ToxicVerdictProvider(verdict_repo),
        *feed_gate,
    ]
    analyzer_providers: list = [pattern_provider, *feed_analyzer]
    if sf_settings.web_risk_enabled:
        # Online lookup: analyzer only, never the create path.
        analyzer_providers.append(
            WebRiskProvider(
                http_client,
                api_key=sf_settings.web_risk_api_key,
                api_base=sf_settings.web_risk_api_base,
            )
        )
        log.info("safety_web_risk_enabled")
    # Deep tier (investigation): its own stream, entered only through the
    # admission policy when screening ends unresolved. Requires the queue
    # Redis — investigation makes outbound calls and must never run
    # inline, so without a queue the deep tier is simply off.
    deep_sink: DeepAnalysisSink = NullDeepAnalysisSink()
    admission = None
    if sf_settings.enabled and sf_settings.deep_enabled:
        if queue_redis_for_webhooks is not None:
            deep_sink = RedisStreamDeepAnalysisSink(
                queue_redis_for_webhooks,
                stream=sf_settings.deep_stream,
                maxlen=sf_settings.deep_maxlen,
            )
            admission = AdmissionPolicy(
                queue_redis_for_webhooks,
                daily_budget=sf_settings.deep_daily_budget,
                report_daily_budget=sf_settings.deep_report_daily_budget,
                admit_sweeps=sf_settings.deep_admit_sweeps,
            )
            log.info("safety_deep_sink_enabled", stream=sf_settings.deep_stream)
        else:
            log.warning(
                "safety_deep_unconfigured",
                detail="deep analysis needs CLICK_EVENTS_QUEUE_REDIS_URI",
            )
    safety_analyzer = SafetyAnalyzer(
        analyzer_providers,
        verdict_repo,
        safety_enforcer,
        ops_notifier,
        reverdict_ttl_hours=sf_settings.reverdict_ttl_hours,
        admission=admission,
        deep_sink=deep_sink if admission is not None else None,
    )
    app.state.safety_analyzer = safety_analyzer
    safety_sink: SafetySink
    if not sf_settings.enabled:
        safety_sink = NullSafetySink()
    elif queue_redis_for_webhooks is not None:
        safety_sink = RedisStreamSafetySink(
            queue_redis_for_webhooks,
            stream=sf_settings.stream,
            maxlen=sf_settings.maxlen,
        )
        log.info("safety_stream_sink_enabled", stream=sf_settings.stream)
    else:
        safety_sink = InlineSafetySink(safety_analyzer, background=True)
        log.info("safety_inline_sink_enabled")
    app.state.safety_sink = safety_sink
    # L1 creation-pattern scoring: counters need the durable queue Redis
    # (the cache Redis would evict them); without it, or with safety off,
    # record_create degrades to a no-op.
    pattern_scorer = None
    if (
        sf_settings.enabled
        and sf_settings.l1_enabled
        and queue_redis_for_webhooks is not None
    ):
        pattern_scorer = CreationPatternScorer(
            queue_redis_for_webhooks,
            safety_sink,
            ops_notifier,
            burst_window_seconds=sf_settings.l1_burst_window_seconds,
            domain_burst_threshold=sf_settings.l1_domain_burst_threshold,
            domain_daily_threshold=sf_settings.l1_domain_daily_threshold,
        )
        log.info("safety_l1_scoring_enabled")
    elif sf_settings.enabled and sf_settings.l1_enabled:
        log.warning(
            "safety_l1_scoring_unconfigured",
            detail="pattern scoring needs CLICK_EVENTS_QUEUE_REDIS_URI",
        )
    url_policy = UrlPolicyService(
        gate_providers,
        blocked_self_domains=settings.blocked_self_domains,
        public_messages=feed_messages,
        scorer=pattern_scorer,
        # Redirect probe; off with the safety master switch.
        redirect_feed_repo=feed_domain_repo if sf_settings.enabled else None,
        redirect_sink=safety_sink if sf_settings.enabled else None,
    )
    app.state.url_policy = url_policy
    # ── Scheduled tasks ──────────────────────────────────────────────
    # Mongo-lease runner (see services/scheduler). Same runtime rule as
    # the webhook executor: embedded in this process when explicitly
    # requested or (auto) when no worker exists in the deploy — queue
    # Redis presence is the worker proxy. app.py starts/cancels the task.
    sch_settings = settings.scheduler
    # Feature tasks from the safety catalogs: feed syncs (each carrying
    # the feed-delta sweep) plus the scheduled sweeps.
    delta_sweeper = FeedDeltaSweeper(url_repo, safety_sink)
    task_registry = build_task_registry(
        [
            *build_feed_tasks(
                sf_settings, http_client, feed_domain_repo, delta_sweeper
            ),
            *build_sweep_tasks(
                sf_settings,
                SweepDeps(
                    url_repo=url_repo,
                    verdict_repo=verdict_repo,
                    sink=safety_sink,
                ),
            ),
        ]
    )
    app.state.task_scheduler = TaskScheduler(
        ScheduledTaskRepository(db["scheduled_tasks"]),
        task_registry,
        poll_interval=sch_settings.poll_seconds,
        lease_seconds=sch_settings.lease_seconds,
    )
    app.state.task_scheduler_embedded = sch_settings.enabled and (
        sch_settings.runtime == "embedded"
        or (sch_settings.runtime == "auto" and queue_redis_for_webhooks is None)
    )
    if (
        sch_settings.enabled
        and not app.state.task_scheduler_embedded
        and sch_settings.runtime in ("auto", "worker")
    ):
        # The worker only boots for its OWN features, never for the scheduler alone.
        log.warning(
            "task_scheduler_delegated_to_worker",
            runtime=sch_settings.runtime,
            detail=(
                "scheduler will only run if a worker process is actually "
                "deployed; no worker means nothing polls scheduled tasks"
            ),
        )

    # ── Services ─────────────────────────────────────────────────────────
    app.state.url_service = UrlService(
        url_repo,
        legacy_repo,
        emoji_repo,
        blocked_url_repo,
        url_cache,
        settings.blocked_self_domains,
        system_default_domain=settings.system_default_domain,
        url_policy=url_policy,
        blocked_url_regex_timeout=settings.blocked_url_regex_timeout,
        max_emoji_alias_length=settings.max_emoji_alias_length,
        emoji_accept_max_version=settings.emoji_accept_max_version,
        emoji_generate_max_version=settings.emoji_generate_max_version,
        emoji_generated_alias_length=settings.emoji_generated_alias_length,
        geo_rules_max_countries=settings.geo_rules_max_countries,
        geo_rules_enabled=settings.geo_rules_enabled,
        og_writethrough=og_writethrough,
        edge_kv=edge_kv_client,
        r2_storage=r2_storage,
        meta_image_max_bytes=r2.upload_max_bytes,
        meta_image_sink=meta_image_sink,
        meta_key_secret=settings.secret_key,
        events=app.state.domain_event_sink,
    )
    app.state.bulk_url_service = BulkUrlService(
        url_repo,
        url_cache,
        url_service=app.state.url_service,
        kv=edge_kv_client,
        system_default_domain=settings.system_default_domain,
        og_ttl_seconds=edge.og_ttl_seconds,
        events=app.state.domain_event_sink,
    )
    app.state.stats_service = StatsService(
        click_repo,
        url_repo,
        max_date_range_days=settings.max_date_range_days,
    )
    # One resolver serves BOTH public read-only surfaces (preview + stats)
    # so they can never disagree about which link a code names or what
    # state it is in.
    public_link_resolver = PublicLinkResolver(
        url_repo,
        legacy_repo,
        emoji_repo,
        system_default_domain=settings.system_default_domain,
    )
    app.state.public_preview_service = PublicPreviewService(public_link_resolver)
    app.state.url_expand_service = UrlExpandService(
        blocked_url_repo,
        MetaFetchCache(redis_client, prefix="url_expand"),
        regex_timeout=settings.blocked_url_regex_timeout,
        user_agent=settings.meta_tags.fetch_user_agent,
        http_client=http_client,
        web_risk_api_key=settings.web_risk.api_key,
    )
    app.state.domain_intel_service = DomainIntelService(
        MetaFetchCache(redis_client, prefix="domain_intel", ttl_seconds=86_400),
        http_client,
    )
    app.state.public_stats_service = PublicStatsService(
        public_link_resolver,
        app.state.stats_service,
        max_date_range_days=settings.max_date_range_days,
    )
    # Report intake shares the resolver (existence checks answer from the
    # same generation the redirect serves) and the ops notifier + captcha
    # already built above for ContactService.
    app.state.report_intake_service = ReportIntakeService(
        ReportRepository(db["reports"]),
        ReportSubmissionRepository(db["report_submissions"]),
        public_link_resolver,
        url_repo,
        captcha,
        ops_notifier,
        system_default_domain=settings.system_default_domain,
        safety_sink=safety_sink,
    )
    app.state.export_service = ExportService(
        app.state.stats_service,
        default_formatters(),
    )
    app.state.api_key_service = ApiKeyService(
        api_key_repo,
        max_active_keys=settings.max_active_api_keys,
    )
    app.state.page_layout_service = PageLayoutService(page_layout_repo)
    token_factory = TokenFactory(settings.jwt)
    otp_service = OtpService(token_repo)

    app.state.user_repo = user_repo
    app.state.token_factory = token_factory

    app.state.credential_service = CredentialService(
        user_repo,
        otp_service,
        app.state.email_provider,
        token_factory,
        account_password_min_length=settings.account_password_min_length,
        account_password_max_length=settings.account_password_max_length,
    )
    app.state.verification_service = EmailVerificationService(
        user_repo,
        otp_service,
        app.state.email_provider,
        token_factory,
    )
    app.state.password_service = PasswordService(
        user_repo,
        otp_service,
        app.state.email_provider,
        account_password_min_length=settings.account_password_min_length,
        account_password_max_length=settings.account_password_max_length,
    )
    app.state.device_auth_service = DeviceAuthService(
        user_repo,
        token_repo,
        token_factory,
        app_grant_repo,
        app_registry=getattr(app.state, "app_registry", None),
    )
    app.state.oauth_service = OAuthService(
        user_repo,
        token_factory,
        app.state.email_provider,
    )
    app.state.profile_picture_service = ProfilePictureService(
        user_repo,
        r2_storage=r2_storage,
        upload_max_bytes=r2.upload_max_bytes,
        key_secret=settings.secret_key,
    )
    app.state.contact_service = ContactService(
        ops_notifier,
        captcha,
    )

    app.state.click_service = build_click_service(
        click_repo,
        url_repo,
        legacy_repo,
        emoji_repo,
        app.state.geoip,
        url_cache,
        events=app.state.domain_event_sink,
    )

    # ── Click event sink ─────────────────────────────────────────────
    # inline (default): classic synchronous tracking, unchanged.
    # stream: XADD to the click stream; the click worker consumes it.
    # Misconfigured stream mode (missing/unreachable queue Redis) degrades
    # to inline with a startup warning — same graceful pattern as custom
    # domains and the optional cache Redis.
    inline_sink = InlineSink(app.state.click_service)
    ce_settings = settings.click_events
    queue_redis = getattr(app.state, "queue_redis", None)
    if ce_settings.sink == "stream" and queue_redis is not None:
        app.state.click_sink = RedisStreamSink(
            queue_redis,
            stream=ce_settings.stream,
            maxlen=ce_settings.maxlen,
            fallback=inline_sink,
        )
        log.info("click_sink_stream_enabled", stream=ce_settings.stream)
    else:
        if ce_settings.sink == "stream":
            log.warning(
                "click_events_stream_unconfigured",
                detail=(
                    "CLICK_EVENTS_SINK=stream but the queue Redis is missing "
                    "or unreachable — falling back to inline click tracking. "
                    "Set CLICK_EVENTS_QUEUE_REDIS_URI to a dedicated Redis."
                ),
            )
        app.state.click_sink = inline_sink

    app.state.app_grant_repo = app_grant_repo

    app.state.feature_flag_service = FeatureFlagService(
        feature_flag_repo, feature_flag_cache
    )

    # Whenever clicks are tracked inline, link.clicked webhooks must fan
    # out at emit time — the worker's stream group only sees clicks that
    # ride the stream. Keying on the CLICK sink (not the domain sink)
    # covers both the Mongo-only rung AND the queue-Redis-present-but-
    # CLICK_EVENTS_SINK=inline combination, which would otherwise leave
    # click webhooks silently unfired.
    if wh_settings.enabled and isinstance(app.state.click_sink, InlineSink):
        app.state.click_sink = WebhookFanoutClickSink(
            app.state.click_sink,
            webhook_dispatcher,
            app.state.geoip,
            settings.system_default_domain,
        )

    # ── Custom-domains feature ───────────────────────────────────────
    # Wired unconditionally so the data plumbing is in place even when the
    # master flag is off. Mutations short-circuit inside the service via
    # ``settings.custom_domains.enabled``; the route layer further gates
    # per-user access via the FeatureFlagService.
    #
    # Backend: one CfSaasBackend instance fills all three protocol slots
    # (verifier, registrar, edge provisioner). If ``cf_zone_id`` is unset,
    # the service still constructs but its mutating paths no-op — operators
    # who haven't configured CF get a feature that registers as "off" via
    # ``custom_domains.enabled`` instead of crashing at boot.
    custom_domain_repo = CustomDomainRepository(db["custom_domains"])
    blocked_domain_repo = BlockedDomainRepository(db["blocked_domains"])
    cd_settings = settings.custom_domains

    # Surface the "enabled but unconfigured" misconfig in startup logs so
    # operators don't have to wait for a request-time 500 to find out.
    # We still boot — the feature just no-ops until creds land.
    if (
        cd_settings.enabled
        and not cd_settings.mock_dcv
        and not (cd_settings.cf_zone_id and cd_settings.cf_api_token)
    ):
        log.warning(
            "custom_domains_enabled_but_unconfigured",
            detail=(
                "custom_domains.enabled=True but cf_zone_id/cf_api_token "
                "unset — feature will fail at request time until configured."
            ),
        )

    if cd_settings.mock_dcv:
        # Local-dev stand-in: same protocol slots, no CF. register() serves
        # the prod-shaped CNAME + ownership TXT; verify() always succeeds.
        log.warning(
            "custom_domains_mock_dcv_active",
            detail=(
                "CUSTOM_DOMAINS_MOCK_DCV=true — domain verification is "
                "mocked and always succeeds. Never enable in production."
            ),
        )
        mock_backend = MockDcvBackend(cname_target=cd_settings.cf_cname_target)
        verifiers = {
            VerificationMethod.CF_DELEGATED_DCV: mock_backend,
            VerificationMethod.CF_HTTP_DCV: mock_backend,
        }
        edge_provisioner = mock_backend
        registrar = mock_backend
    else:
        # CloudflareClient takes Optional[str] and only raises
        # CloudflareNotConfiguredError on first request, never at construction —
        # so the operator-hasn't-set-CF-up case is fine to wire here.
        cf_client = CloudflareClient(
            http_client=http_client,
            api_token=cd_settings.cf_api_token,
            zone_id=cd_settings.cf_zone_id,
            max_retries=cd_settings.cf_api_max_retries,
            initial_backoff_seconds=cd_settings.cf_api_initial_backoff_seconds,
        )
        cf_backend = CfSaasBackend(
            cf_client=cf_client,
            custom_domain_repo=custom_domain_repo,
            cname_target=cd_settings.cf_cname_target,
            dcv_delegation_target=cd_settings.cf_dcv_delegation_target,
        )
        verifiers = {
            VerificationMethod.CF_DELEGATED_DCV: cf_backend,
            VerificationMethod.CF_HTTP_DCV: cf_backend,
        }
        edge_provisioner = cf_backend
        registrar = cf_backend

    # Build the resolver before the service so the service can take it as a dep
    tenant_resolver = CachedMongoTenantResolver(
        repo=custom_domain_repo,
        redis_client=redis_client,
        system_default_domain=settings.system_default_domain,
    )
    app.state.tenant_resolver = tenant_resolver
    app.state.custom_domain_service = CustomDomainService(
        repo=custom_domain_repo,
        verifiers=verifiers,
        edge_provisioner=edge_provisioner,
        registrar=registrar,
        settings=cd_settings,
        tenant_resolver=tenant_resolver,
        blocked_domain_repo=blocked_domain_repo,
        redis_client=redis_client,
        # Mock DCV must also skip the real-DNS preflight in verify(), or
        # local domains would fail the CNAME lookup before the mock runs.
        preflight_cname_target=cd_settings.cf_cname_target
        if cd_settings.cf_zone_id and not cd_settings.mock_dcv
        else None,
        url_service=app.state.url_service,
    )
