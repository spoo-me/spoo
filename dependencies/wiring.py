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
from infrastructure.cache.entitlement_cache import EntitlementCache
from infrastructure.cache.feature_flag_cache import FeatureFlagCache
from infrastructure.cache.meta_fetch_cache import MetaFetchCache
from infrastructure.cache.onboarding_cache import OnboardingCache
from infrastructure.cache.url_cache import UrlCache
from infrastructure.cache.web_risk_budget import WebRiskBudget
from infrastructure.captcha.hcaptcha import HCaptchaProvider
from infrastructure.cloudflare_client import CloudflareClient
from infrastructure.cloudflare_kv import CloudflareKVClient
from infrastructure.email.zeptomail import ZeptoMailProvider
from infrastructure.http_client import HttpClient
from infrastructure.logging import get_logger
from infrastructure.ops_notify import DiscordOpsNotifier
from infrastructure.posthog_erasure import HttpPostHogEraser
from infrastructure.storage.r2 import R2StorageClient
from infrastructure.web_risk import (
    DISPLAY_THREAT_TYPES,
    ENFORCEMENT_THREAT_TYPES,
    WebRiskClient,
)
from repositories.api_key_repository import ApiKeyRepository
from repositories.app_grant_repository import AppGrantRepository
from repositories.billing_event_repository import BillingEventRepository
from repositories.blocked_domain_repository import BlockedDomainRepository
from repositories.blocked_url_repository import BlockedUrlRepository
from repositories.click_repository import ClickRepository
from repositories.custom_domain_repository import CustomDomainRepository
from repositories.entitlement_event_repository import EntitlementEventRepository
from repositories.entitlement_override_repository import (
    EntitlementOverrideRepository,
)
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
from repositories.subscription_repository import SubscriptionRepository
from repositories.tag_repository import TagRepository
from repositories.token_repository import TokenRepository
from repositories.url_repository import UrlRepository
from repositories.user_repository import UserRepository
from repositories.verdict_repository import VerdictRepository
from repositories.webhook_delivery_repository import WebhookDeliveryRepository
from repositories.webhook_endpoint_repository import WebhookEndpointRepository
from repositories.webhook_event_repository import WebhookEventRepository
from schemas.enums.domain_status import VerificationMethod
from services.account_deletion_service import AccountDeletionService
from services.account_erasure_service import (
    AccountErasureService,
    ErasureMailer,
    NoopErasureMailer,
    NoopPostHogEraser,
    PostHogEraser,
    erasure_sweep_task,
)
from services.api_key_service import ApiKeyService
from services.auth.credentials import CredentialService
from services.auth.device import DeviceAuthService
from services.auth.otp import OtpService
from services.auth.password import PasswordService
from services.auth.verification import EmailVerificationService
from services.billing import (
    BillingProvider,
    BillingService,
    NullBillingProvider,
    PaddleProvider,
)
from services.bulk_url_service import BulkUrlService
from services.cf_saas_backend import CfSaasBackend
from services.click import ClickService, LegacyClickHandler, V2ClickHandler
from services.click.sinks import InlineSink, RedisStreamSink
from services.contact_service import ContactService
from services.custom_domain_service import CustomDomainService
from services.domain_intel_service import DomainIntelService
from services.edge_cache.og_writethrough import OgEdgeWritethrough
from services.entitlements import EntitlementService
from services.entitlements.lifecycle import LifecycleService, lifecycle_tasks
from services.entitlements.over_limit import OverLimitService
from services.events.sinks import (
    InlineDomainEventSink,
    NullDomainEventSink,
    StreamDomainEventSink,
)
from services.export.formatters import default_formatters
from services.export.service import ExportService
from services.feature_flag_service import FeatureFlagService
from services.features.catalog import validate_override
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
    CARRIER_FEEDS,
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
    SafetyNotifier,
    SafetySink,
    SharedCarrierLookup,
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
from services.tag_service import TagService
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

# The expander answers a waiting request, so it can't sit on the safety
# analyzer's queue-side timeout.
_EXPANDER_WEB_RISK_TIMEOUT = 4.0


def build_expander_web_risk(
    settings: AppSettings, http_client: HttpClient
) -> WebRiskClient | None:
    """The URL expander's Web Risk client, on the same credential as the
    safety analyzer. Its threat list is wider because this verdict is
    displayed, never enforced.
    """
    safety = settings.safety
    if not safety.web_risk_enabled:
        return None
    return WebRiskClient(
        http_client,
        api_key=safety.web_risk_api_key,
        api_base=safety.web_risk_api_base,
        threat_types=DISPLAY_THREAT_TYPES,
        timeout=_EXPANDER_WEB_RISK_TIMEOUT,
    )


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


def build_r2_storage(settings: AppSettings, http_client) -> R2StorageClient | None:
    """R2 client when fully configured; None otherwise — shared by the app
    wiring and the worker's erasure factory.

    The client's ``__init__`` rejects plain-http endpoints outside loopback
    (SigV4 signs, never encrypts). A self-host compose pointing R2 at
    ``http://minio:9000`` must still boot, so that rejection degrades here
    to the same disabled state as unconfigured R2 — mirroring how every
    other optional integration degrades — instead of crashing startup.
    """
    r2 = settings.r2
    if not r2.enabled:
        return None
    try:
        return R2StorageClient(
            http_client=http_client,
            account_id=r2.account_id,
            access_key_id=r2.access_key_id,
            secret_access_key=r2.secret_access_key,
            bucket=r2.bucket,
            public_base_url=r2.public_base_url,
            endpoint_url=r2.endpoint_url,
            request_timeout_seconds=r2.request_timeout_seconds,
        )
    except ValueError as exc:
        log.warning(
            "r2_storage_disabled_insecure_endpoint",
            endpoint=r2.endpoint_url,
            reason=str(exc),
        )
        return None


def build_erasure_mailer(
    settings: AppSettings,
    http_client,
    email_provider: ZeptoMailProvider | None = None,
) -> ErasureMailer:
    """Deletion lifecycle mail: ZeptoMail when configured, Noop otherwise.

    The app process passes its existing ``email_provider`` singleton (one
    Jinja environment, one client); the worker passes None and gets a
    fresh provider. Unconfigured token ⇒ Noop, so the deletion paths never
    log per-send "token_not_configured" errors on mail-less deployments.
    """
    if not settings.email.zepto_api_token:
        return NoopErasureMailer()
    return email_provider or ZeptoMailProvider(
        settings.email, http_client, app_url=settings.app_url
    )


def build_posthog_eraser(settings: AppSettings, http_client) -> PostHogEraser:
    """PostHog person deletion: HTTP eraser when configured, Noop otherwise.

    Env-gated on POSTHOG_ERASURE_API_KEY + POSTHOG_ERASURE_PROJECT_ID —
    self-hosts without PostHog skip the step entirely.
    """
    ph = settings.posthog_erasure
    if not ph.enabled:
        return NoopPostHogEraser()
    return HttpPostHogEraser(
        http_client,
        api_key=ph.api_key,
        project_id=ph.project_id,
        host=ph.host,
    )


def build_entitlement_store(
    db, redis_client
) -> tuple[
    EntitlementCache,
    EntitlementEventRepository,
    SubscriptionRepository,
    EntitlementOverrideRepository,
]:
    """The three entitlement repositories sharing one cache, so every write
    to subscriptions or overrides invalidates the same ``ent:{id}`` key."""
    cache = EntitlementCache(redis_client)
    events = EntitlementEventRepository(db["entitlement_events"])
    subscriptions = SubscriptionRepository(db["subscriptions"], events, cache)
    overrides = EntitlementOverrideRepository(
        db["entitlement_overrides"], events, cache, check=validate_override
    )
    return cache, events, subscriptions, overrides


def build_entitlement_service(
    db, settings: AppSettings, redis_client, *, store=None
) -> EntitlementService:
    cache, events, subscriptions, overrides = store or build_entitlement_store(
        db, redis_client
    )
    domain_repo = CustomDomainRepository(db["custom_domains"])
    endpoint_repo = WebhookEndpointRepository(db["webhook-endpoints"])
    key_repo = ApiKeyRepository(db["api-keys"])
    reconciler = OverLimitService(
        endpoints=endpoint_repo,
        domains=domain_repo,
        keys=key_repo,
        tenant_resolver=CachedMongoTenantResolver(
            repo=domain_repo,
            redis_client=redis_client,
            system_default_domain=settings.system_default_domain,
        ),
        webhook_owner_cache=OwnerSubscriptionCache(
            redis_client, ttl_seconds=settings.webhooks.matcher_cache_ttl_seconds
        ),
    )
    return EntitlementService(
        subscriptions,
        overrides,
        events,
        cache,
        selfhost=settings.billing.selfhost,
        usage={
            "custom_domains_max": domain_repo.count_by_owner,
            "webhook_endpoints_max": endpoint_repo.count_by_user,
            "api_keys_max": key_repo.count_by_user,
        },
        over_limit=reconciler,
    )


def build_billing_provider(settings: AppSettings, http_client) -> BillingProvider:
    billing = settings.billing
    if billing.provider == "paddle":
        return PaddleProvider(
            http_client,
            api_key=billing.paddle_api_key,
            webhook_secret=billing.paddle_webhook_secret,
            env=billing.paddle_env,
        )
    return NullBillingProvider()


def build_billing_service(
    db,
    settings: AppSettings,
    redis_client,
    *,
    http_client,
    entitlements: EntitlementService,
    subscriptions: SubscriptionRepository,
) -> BillingService:
    billing = settings.billing
    return BillingService(
        build_billing_provider(settings, http_client),
        entitlements,
        subscriptions,
        BillingEventRepository(db["billing_events"]),
        redis_client,
        billing,
        app_url=settings.app_url,
    )


def build_lifecycle_service(
    db,
    settings: AppSettings,
    *,
    store,
    entitlements: EntitlementService,
    provider: BillingProvider,
    mailer,
) -> LifecycleService:
    """``store`` is the caller's entitlement store tuple. An unconfigured
    mailer becomes None so the job never retries a send it cannot make."""
    _, events, subscriptions, overrides = store
    return LifecycleService(
        entitlements,
        subscriptions,
        overrides,
        events,
        UserRepository(db["users"]),
        CustomDomainRepository(db["custom_domains"]),
        FeatureFlagRepository(db["feature_flags"]),
        mailer if settings.email.zepto_api_token else None,
        app_url=settings.app_url,
        provider=provider,
    )


def build_account_erasure_service(
    db,
    settings: AppSettings,
    http_client,
    redis_client,
) -> AccountErasureService:
    """Erasure cascade for the click worker's scheduler runtime.

    The app process wires ``AccountErasureService`` from the singletons in
    ``wire_services``; the worker has no ``app.state``, so this composes an
    erasure-grade graph from primitives. Every deletion side effect (URL
    cache invalidate, edge-KV purge / og write-through removal, CF custom
    hostname cascade, R2 sweep) is wired identically to the app — creation-
    side deps (captcha, meta sinks, event fanout) are irrelevant to deletes
    and stay off. ``redis_client`` may be None: cache invalidation then
    degrades to no-ops, same as everywhere else in the worker.
    """
    url_cache = UrlCache(redis_client, ttl_seconds=settings.redis.redis_ttl_seconds)

    edge = settings.edge_cache
    edge_kv_client = None
    og_writethrough = None
    if edge.enabled:
        edge_kv_client = CloudflareKVClient(
            http_client=http_client,
            api_token=edge.cf_api_token,
            account_id=edge.cf_account_id,
            namespace_id=edge.kv_namespace_id,
            api_base=edge.api_base,
            api_host_header=edge.api_host_header,
        )
        og_writethrough = OgEdgeWritethrough(
            edge_kv_client,
            system_domain=settings.system_default_domain,
            ttl_seconds=edge.og_ttl_seconds,
        )

    r2_storage = build_r2_storage(settings, http_client)

    user_repo = UserRepository(db["users"])
    url_service = UrlService(
        UrlRepository(db["urlsV2"]),
        LegacyUrlRepository(db["urls"]),
        EmojiUrlRepository(db["emojis"]),
        BlockedUrlRepository(db["blocked-urls"]),
        url_cache,
        settings.blocked_self_domains,
        system_default_domain=settings.system_default_domain,
        # Creation-side gate, unreachable from erasure's delete paths —
        # provider-less keeps the safety stack out of the worker.
        url_policy=UrlPolicyService(
            [], blocked_self_domains=settings.blocked_self_domains
        ),
        og_writethrough=og_writethrough,
        edge_kv=edge_kv_client,
        r2_storage=r2_storage,
        meta_key_secret=settings.secret_key,
        user_repo=user_repo,
    )

    # Custom-domains stack, mirrored from wire_services: erasure needs the
    # edge provisioner (announce_revoked) for the CF cascade. Mock backend
    # under CUSTOM_DOMAINS_MOCK_DCV keeps local stacks CF-free.
    cd_settings = settings.custom_domains
    custom_domain_repo = CustomDomainRepository(db["custom_domains"])
    if cd_settings.mock_dcv:
        backend = MockDcvBackend(cname_target=cd_settings.cf_cname_target)
    else:
        backend = CfSaasBackend(
            cf_client=CloudflareClient(
                http_client=http_client,
                api_token=cd_settings.cf_api_token,
                zone_id=cd_settings.cf_zone_id,
                max_retries=cd_settings.cf_api_max_retries,
                initial_backoff_seconds=cd_settings.cf_api_initial_backoff_seconds,
            ),
            custom_domain_repo=custom_domain_repo,
            cname_target=cd_settings.cf_cname_target,
            dcv_delegation_target=cd_settings.cf_dcv_delegation_target,
        )
    domain_service = CustomDomainService(
        repo=custom_domain_repo,
        verifiers={
            VerificationMethod.CF_DELEGATED_DCV: backend,
            VerificationMethod.CF_HTTP_DCV: backend,
        },
        edge_provisioner=backend,
        registrar=backend,
        settings=cd_settings,
        tenant_resolver=CachedMongoTenantResolver(
            repo=custom_domain_repo,
            redis_client=redis_client,
            system_default_domain=settings.system_default_domain,
        ),
        redis_client=redis_client,
        url_service=url_service,
    )
    _, ent_events, subscription_repo, override_repo = build_entitlement_store(
        db, redis_client
    )

    return AccountErasureService(
        user_repo=user_repo,
        url_service=url_service,
        domain_service=domain_service,
        tag_service=TagService(TagRepository(db["tags"]), UrlRepository(db["urlsV2"])),
        click_repo=ClickRepository(db["clicks"]),
        api_key_repo=ApiKeyRepository(db["api-keys"]),
        token_repo=TokenRepository(db["verification-tokens"]),
        page_layout_repo=PageLayoutRepository(db["page-layouts"]),
        app_grant_repo=AppGrantRepository(db["app-grants"]),
        webhook_endpoint_repo=WebhookEndpointRepository(db["webhook-endpoints"]),
        webhook_event_repo=WebhookEventRepository(db["webhook-events"]),
        webhook_delivery_repo=WebhookDeliveryRepository(db["webhook-deliveries"]),
        report_repo=ReportRepository(db["reports"]),
        report_submission_repo=ReportSubmissionRepository(db["report_submissions"]),
        feature_flag_repo=FeatureFlagRepository(db["feature_flags"]),
        subscription_repo=subscription_repo,
        override_repo=override_repo,
        entitlement_event_repo=ent_events,
        r2_storage=r2_storage,
        posthog=build_posthog_eraser(settings, http_client),
        mailer=build_erasure_mailer(settings, http_client),
        key_secret=settings.secret_key,
        batch_limit=settings.account_erasure_batch_limit,
        time_budget_seconds=settings.account_erasure_time_budget_seconds,
        claim_lease_seconds=settings.account_erasure_claim_lease_seconds,
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
    ent_store = build_entitlement_store(db, redis_client)
    _, ent_events, subscription_repo, override_repo = ent_store
    app.state.entitlement_service = build_entitlement_service(
        db, settings, redis_client, store=ent_store
    )
    app.state.billing_service = build_billing_service(
        db,
        settings,
        redis_client,
        http_client=http_client,
        entitlements=app.state.entitlement_service,
        subscriptions=subscription_repo,
    )

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
    r2 = settings.r2
    r2_storage = build_r2_storage(settings, http_client)
    if r2_storage is not None:
        log.info("r2_storage_enabled", bucket=r2.bucket)
    elif not r2.enabled and any(
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
        concurrency=wh_settings.executor_concurrency,
        per_endpoint_concurrency=wh_settings.executor_per_endpoint_concurrency,
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
                WebRiskClient(
                    http_client,
                    api_key=sf_settings.web_risk_api_key,
                    api_base=sf_settings.web_risk_api_base,
                    threat_types=ENFORCEMENT_THREAT_TYPES,
                )
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
        SafetyNotifier(ops_notifier),
        reverdict_ttl_hours=sf_settings.reverdict_ttl_hours,
        admission=admission,
        deep_sink=deep_sink if admission is not None else None,
        carriers=SharedCarrierLookup(feed_domain_repo, feeds=CARRIER_FEEDS),
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
    # ── Services ─────────────────────────────────────────────────────────
    tag_repo = TagRepository(db["tags"])
    app.state.tag_service = TagService(tag_repo, url_repo)
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
        ab_variants_max=settings.ab_variants_max,
        ab_variants_enabled=settings.ab_variants_enabled,
        og_writethrough=og_writethrough,
        edge_kv=edge_kv_client,
        r2_storage=r2_storage,
        meta_image_max_bytes=r2.upload_max_bytes,
        meta_image_sink=meta_image_sink,
        meta_key_secret=settings.secret_key,
        events=app.state.domain_event_sink,
        user_repo=user_repo,
        tag_service=app.state.tag_service,
        entitlements=app.state.entitlement_service,
    )
    app.state.bulk_url_service = BulkUrlService(
        url_repo,
        url_cache,
        url_service=app.state.url_service,
        kv=edge_kv_client,
        system_default_domain=settings.system_default_domain,
        og_ttl_seconds=edge.og_ttl_seconds,
        events=app.state.domain_event_sink,
        tag_service=app.state.tag_service,
    )
    app.state.stats_service = StatsService(
        click_repo,
        url_repo,
        tag_service=app.state.tag_service,
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
    app.state.feature_flag_service = FeatureFlagService(
        feature_flag_repo, feature_flag_cache
    )

    app.state.public_preview_service = PublicPreviewService(
        public_link_resolver,
        app.state.entitlement_service,
        app.state.feature_flag_service,
    )
    app.state.url_expand_service = UrlExpandService(
        blocked_url_repo,
        MetaFetchCache(redis_client, prefix="url_expand"),
        regex_timeout=settings.blocked_url_regex_timeout,
        user_agent=settings.meta_tags.fetch_user_agent,
        web_risk=build_expander_web_risk(settings, http_client),
        web_risk_budget=WebRiskBudget(
            redis_client, limit=settings.safety.web_risk_expander_daily_budget
        ),
    )
    app.state.domain_intel_service = DomainIntelService(
        MetaFetchCache(redis_client, prefix="domain_intel", ttl_seconds=86_400),
        http_client,
    )
    app.state.public_stats_service = PublicStatsService(
        public_link_resolver,
        app.state.stats_service,
        app.state.entitlement_service,
        app.state.feature_flag_service,
    )
    # Report intake shares the resolver (existence checks answer from the
    # same generation the redirect serves) and the ops notifier + captcha
    # already built above for ContactService. The repos are locals so the
    # account-erasure cascade below shares the same instances.
    report_repo = ReportRepository(db["reports"])
    report_submission_repo = ReportSubmissionRepository(db["report_submissions"])
    app.state.report_intake_service = ReportIntakeService(
        report_repo,
        report_submission_repo,
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
    )
    app.state.page_layout_service = PageLayoutService(page_layout_repo)
    token_factory = TokenFactory(
        settings.jwt, plan_of=app.state.entitlement_service.plan_hint_for
    )
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
    # Deletion intake + grace-period restore. One mailer instance shared
    # with the erasure cascade below: the ZeptoMail singleton when the
    # token is configured, Noop otherwise.
    erasure_mailer = build_erasure_mailer(
        settings, http_client, email_provider=app.state.email_provider
    )
    app.state.account_deletion_service = AccountDeletionService(
        user_repo,
        token_repo=token_repo,
        mailer=erasure_mailer,
        grace_days=settings.account_deletion_grace_days,
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

    # ── Account erasure ──────────────────────────────────────────────
    # GDPR Art. 17 cascade, driven by the erasure sweep below. Reuses the
    # singletons built above so deletion side effects (cache invalidate,
    # edge purge, CF hostname cascade, R2 sweep) match the interactive
    # paths exactly. PostHog and the confirmation mail are env-gated —
    # Noop when their settings are unconfigured.
    account_erasure_service = AccountErasureService(
        user_repo=user_repo,
        url_service=app.state.url_service,
        domain_service=app.state.custom_domain_service,
        tag_service=app.state.tag_service,
        click_repo=click_repo,
        api_key_repo=api_key_repo,
        token_repo=token_repo,
        page_layout_repo=page_layout_repo,
        app_grant_repo=app_grant_repo,
        webhook_endpoint_repo=webhook_endpoint_repo,
        webhook_event_repo=webhook_event_repo,
        webhook_delivery_repo=webhook_delivery_repo,
        report_repo=report_repo,
        report_submission_repo=report_submission_repo,
        feature_flag_repo=feature_flag_repo,
        subscription_repo=subscription_repo,
        override_repo=override_repo,
        entitlement_event_repo=ent_events,
        r2_storage=r2_storage,
        posthog=build_posthog_eraser(settings, http_client),
        mailer=erasure_mailer,
        key_secret=settings.secret_key,
        batch_limit=settings.account_erasure_batch_limit,
        time_budget_seconds=settings.account_erasure_time_budget_seconds,
        claim_lease_seconds=settings.account_erasure_claim_lease_seconds,
    )

    # ── Scheduled tasks ──────────────────────────────────────────────
    # Mongo-lease runner (see services/scheduler). Same runtime rule as
    # the webhook executor: embedded in this process when explicitly
    # requested or (auto) when no worker exists in the deploy — queue
    # Redis presence is the worker proxy. app.py starts/cancels the task.
    # Feature tasks are constructed here with their deps closed over; the
    # click worker registers its own instances (workers/click_worker.py)
    # so the task exists in whichever process hosts the runner.
    sch_settings = settings.scheduler
    # Feature tasks from the safety catalogs (feed syncs, each carrying
    # the feed-delta sweep, plus the scheduled sweeps) and the account
    # erasure sweep. One registry — the runner claims only names in it.
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
            erasure_sweep_task(account_erasure_service),
            *lifecycle_tasks(
                build_lifecycle_service(
                    db,
                    settings,
                    store=ent_store,
                    entitlements=app.state.entitlement_service,
                    provider=build_billing_provider(settings, http_client),
                    mailer=app.state.email_provider,
                )
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
