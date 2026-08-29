"""Click event worker — consumes the click stream with FastStream.

Run with (compose does this):

    uv run uvicorn --factory workers.click_worker:create_app \
        --host 0.0.0.0 --port 8001

One process hosts every enabled consumer group. Per group, TWO FastStream
subscribers are registered against the same stream:

- **reader**  — normal ``XREADGROUP`` consumption of new messages.
- **claimer** — ``XAUTOCLAIM`` recovery (``min_idle_time``) of messages a
  dead/stuck consumer never acked. FastStream 0.7 makes these two modes
  mutually exclusive per subscriber, hence the pair. The claim path is
  fronted by :class:`ClaimDeadLetterGuard` so poison messages land in the
  DLQ after ``max_deliveries`` attempts instead of looping forever.

Both subscribers delegate to the same framework-free consumer class
(``StatsClickConsumer`` / ``HotUrlDetector``), so processing logic stays
identical regardless of which path delivered the message. Handler
exceptions leave the message pending (FastStream's default
``REJECT_ON_ERROR`` policy for group subscribers = no XACK), which is
what feeds the claimer.

Groups hosted by this process = ``CLICK_EVENTS_WORKER_GROUPS`` filtered by
feature toggles (``hotness`` also needs ``CLICK_EVENTS_HOTNESS_ENABLED``).
A future deployment can split groups across containers by setting
``CLICK_EVENTS_WORKER_GROUPS='["stats"]'`` etc. — no code change.

``GET /health`` (ASGI) pings the broker — wired as the container
healthcheck.
"""

from __future__ import annotations

import asyncio
import os
import socket
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv
from faststream.asgi import AsgiFastStream, make_ping_asgi
from faststream.redis import RedisBroker, StreamSub
from faststream.redis.annotations import Redis, RedisMessage
from pymongo.asynchronous.mongo_client import AsyncMongoClient

from config import AppSettings, ClickEventsSettings
from dependencies.wiring import build_click_service
from infrastructure.cache.redis_client import create_redis_client
from infrastructure.cache.url_cache import UrlCache
from infrastructure.cloudflare_kv import CloudflareKVClient
from infrastructure.geoip import GeoIPService
from infrastructure.http_client import HttpClient
from infrastructure.llm import LlmTaskRunner
from infrastructure.logging import get_logger, setup_logging
from infrastructure.ops_notify import DiscordOpsNotifier
from repositories.blocked_url_repository import BlockedUrlRepository
from repositories.click_repository import ClickRepository
from repositories.feed_domain_repository import FeedDomainRepository
from repositories.legacy.emoji_url_repository import EmojiUrlRepository
from repositories.legacy.legacy_url_repository import LegacyUrlRepository
from repositories.scheduled_task_repository import ScheduledTaskRepository
from repositories.url_repository import UrlRepository
from repositories.verdict_repository import VerdictRepository
from repositories.webhook_delivery_repository import WebhookDeliveryRepository
from repositories.webhook_endpoint_repository import WebhookEndpointRepository
from repositories.webhook_event_repository import WebhookEventRepository
from services.click.consumers import (
    ClickConsumer,
    HotUrlDetector,
    LogHotUrlAction,
    StatsClickConsumer,
)
from services.click.consumers.hotness import HotUrlAction
from services.edge_cache import PromoteToEdgeCacheAction
from services.edge_cache.og_writethrough import OgEdgeWritethrough
from services.events.contract import DOMAIN_EVENTS_STREAM
from services.events.sinks import InlineDomainEventSink
from services.meta_tags.validator import MetaImageValidator
from services.safety import (
    AdmissionPolicy,
    AutoBlockPolicy,
    BlockedPatternProvider,
    BrowserRunClient,
    DeepAnalysisConsumer,
    DeepInvestigator,
    FeedDeltaSweeper,
    HotLinkScreen,
    InlineSafetySink,
    InvestigationToolDeps,
    RedisStreamDeepAnalysisSink,
    SafetyAnalyzer,
    SafetyEnforcer,
    SweepDeps,
    WebRiskProvider,
    build_feed_providers,
    build_feed_tasks,
    build_investigate_task,
    build_investigation_tools,
    build_sweep_tasks,
)
from services.safety.consumers import SafetyAnalysisConsumer
from services.scheduler import TaskScheduler
from services.scheduler.tasks import build_task_registry
from services.webhooks import (
    DeliveryExecutor,
    OwnerSubscriptionCache,
    SubscriptionMatcher,
    WebhookDispatcher,
)
from services.webhooks.consumers import WebhookClickConsumer, WebhookDomainConsumer
from services.webhooks.renderers import default_renderers
from workers.dlq import ClaimDeadLetterGuard
from workers.telemetry import StaleConsumerJanitor, StreamMetricsReporter

log = get_logger(__name__)

# The worker's Mongo traffic is a fraction of the web app's — small pool.
_WORKER_MONGO_MAX_POOL = 16


@dataclass
class _WorkerRuntime:
    """Connections, consumers, and telemetry built at startup."""

    mongo_client: AsyncMongoClient
    cache_redis: Any | None
    counter_redis: Any | None
    telemetry_redis: Any | None = None
    deep_redis: Any | None = None
    http_client: HttpClient | None = None
    telemetry_tasks: list[asyncio.Task] = field(default_factory=list)
    consumers: dict[str, ClickConsumer] = field(default_factory=dict)
    meta_validator: MetaImageValidator | None = None
    webhook_domain_consumer: WebhookDomainConsumer | None = None
    safety_consumer: SafetyAnalysisConsumer | None = None
    deep_consumer: DeepAnalysisConsumer | None = None

    async def aclose(self) -> None:
        for task in self.telemetry_tasks:
            task.cancel()
        # cancel() is only a request — drain the tasks before closing the
        # connections they may still hold mid-call.
        if self.telemetry_tasks:
            await asyncio.gather(*self.telemetry_tasks, return_exceptions=True)
        await self.mongo_client.close()
        if self.cache_redis is not None:
            await self.cache_redis.aclose()
        if self.counter_redis is not None:
            await self.counter_redis.aclose()
        if self.telemetry_redis is not None:
            await self.telemetry_redis.aclose()
        if self.deep_redis is not None:
            await self.deep_redis.aclose()
        if self.http_client is not None:
            await self.http_client.aclose()


def enabled_groups(ce: ClickEventsSettings) -> list[str]:
    """Groups this process should host: worker_groups ∩ feature toggles."""
    groups = []
    for group in ce.worker_groups:
        if group == "hotness" and not ce.hotness_enabled:
            continue
        groups.append(group)
    return groups


async def _build_runtime(
    settings: AppSettings,
    groups: list[str],
    *,
    run_meta: bool = False,
    run_webhooks: bool = False,
    run_safety: bool = False,
    run_scheduler: bool = False,
) -> _WorkerRuntime:
    ce = settings.click_events
    mongo_client: AsyncMongoClient = AsyncMongoClient(
        settings.db.mongodb_uri,
        maxPoolSize=_WORKER_MONGO_MAX_POOL,
        minPoolSize=1,
    )
    db = mongo_client[settings.db.db_name]

    # Cache Redis is optional in the worker exactly as in the web app —
    # without it the URL cache degrades to no-ops (max-clicks expiry just
    # skips cache invalidation; resolve-side caching is the app's concern).
    cache_redis = None
    if settings.redis.redis_uri:
        cache_redis = await create_redis_client(settings.redis.redis_uri, label="cache")

    runtime = _WorkerRuntime(
        mongo_client=mongo_client,
        cache_redis=cache_redis,
        counter_redis=None,
    )

    # Built FIRST so the stats consumer can carry the sink (link.expired
    # fires from its max-clicks branch). In-process dispatch — same process
    # as the dispatcher, no re-queue round trip.
    worker_domain_sink = None
    worker_safety_sink = None
    if run_webhooks:
        # Webhooks stack: dispatcher fed by two consumer-group
        # feeds (clicks + domain events), executor as a background task —
        # Mongo-only, so it shares this process without extra infra.
        wh = settings.webhooks
        webhook_event_repo = WebhookEventRepository(db["webhook-events"])
        webhook_endpoint_repo = WebhookEndpointRepository(db["webhook-endpoints"])
        webhook_delivery_repo = WebhookDeliveryRepository(db["webhook-deliveries"])
        dispatcher = WebhookDispatcher(
            SubscriptionMatcher(
                webhook_endpoint_repo,
                OwnerSubscriptionCache(
                    cache_redis, ttl_seconds=wh.matcher_cache_ttl_seconds
                ),
            ),
            webhook_event_repo,
            webhook_delivery_repo,
            webhook_endpoint_repo,
            max_pending_per_endpoint=wh.max_pending_per_endpoint,
        )
        worker_domain_sink = InlineDomainEventSink(dispatcher)
        webhook_geoip = GeoIPService(settings.geoip_country_db, settings.geoip_city_db)
        runtime.consumers["webhooks"] = WebhookClickConsumer(
            dispatcher, webhook_geoip, settings.system_default_domain
        )
        runtime.webhook_domain_consumer = WebhookDomainConsumer(dispatcher)
        executor = DeliveryExecutor(
            webhook_delivery_repo,
            webhook_endpoint_repo,
            webhook_event_repo,
            default_renderers(),
            master_secret=settings.secret_key,
            delivery_timeout=wh.delivery_timeout_seconds,
            max_payload_bytes=wh.max_payload_bytes,
            max_consecutive_failures=wh.max_consecutive_failures,
            poll_interval=wh.executor_poll_seconds,
            lease_seconds=wh.executor_lease_seconds,
        )
        runtime.telemetry_tasks.append(
            asyncio.create_task(executor.run(), name="webhook-delivery-executor")
        )
        log.info("webhooks_worker_registered", stream=DOMAIN_EVENTS_STREAM)

    if "stats" in groups:
        geoip = GeoIPService(settings.geoip_country_db, settings.geoip_city_db)
        url_cache = UrlCache(cache_redis, ttl_seconds=settings.redis.redis_ttl_seconds)
        runtime.consumers["stats"] = StatsClickConsumer(
            build_click_service(
                ClickRepository(db["clicks"]),
                UrlRepository(db["urlsV2"]),
                LegacyUrlRepository(db["urls"]),
                EmojiUrlRepository(db["emojis"]),
                geoip,
                url_cache,
                events=worker_domain_sink,
            )
        )

    if "hotness" in groups:
        # Window counters live on the queue Redis (noeviction) — a separate
        # decode_responses client from the broker's internal bytes client.
        counter_redis = await create_redis_client(
            ce.queue_redis_uri, label="hotness-counters"
        )
        if counter_redis is None:
            await runtime.aclose()
            raise RuntimeError(
                "hotness group enabled but the queue Redis is unreachable"
            )
        runtime.counter_redis = counter_redis

        actions: list[HotUrlAction] = [LogHotUrlAction()]
        edge = settings.edge_cache
        if edge.enabled:
            if cache_redis is None:
                # Promotion reads fresh URL state from the URL cache; with
                # no cache Redis every lookup would miss and every hot URL
                # would be skipped — surface the config error instead.
                log.warning(
                    "edge_cache_disabled",
                    detail="EDGE_CACHE_* is set but REDIS_URI is not — "
                    "promotion needs the URL cache. Skipping registration.",
                )
            else:
                http_client = HttpClient(timeout=settings.http_client_timeout)
                runtime.http_client = http_client
                actions.append(
                    PromoteToEdgeCacheAction(
                        UrlCache(
                            cache_redis,
                            ttl_seconds=settings.redis.redis_ttl_seconds,
                        ),
                        CloudflareKVClient(
                            http_client=http_client,
                            api_token=edge.cf_api_token,
                            account_id=edge.cf_account_id,
                            namespace_id=edge.kv_namespace_id,
                            api_base=edge.api_base,
                            api_host_header=edge.api_host_header,
                        ),
                        system_domain=settings.system_default_domain,
                        ttl_seconds=edge.ttl_seconds,
                        ttl_jitter_ratio=edge.ttl_jitter_ratio,
                    )
                )
                log.info(
                    "edge_promotion_registered",
                    kv_namespace_id=edge.kv_namespace_id,
                    ttl_seconds=edge.ttl_seconds,
                )

        runtime.consumers["hotness"] = HotUrlDetector(
            counter_redis,
            threshold=ce.hot_threshold,
            window_seconds=ce.hot_window_seconds,
            actions=actions,
        )

    if run_meta:
        # Async og:image validation for custom meta-tags — own stream/group.
        mt = settings.meta_tags
        edge = settings.edge_cache
        og_writethrough = None
        if edge.enabled:
            if runtime.http_client is None:
                runtime.http_client = HttpClient(timeout=settings.http_client_timeout)
            og_writethrough = OgEdgeWritethrough(
                CloudflareKVClient(
                    http_client=runtime.http_client,
                    api_token=edge.cf_api_token,
                    account_id=edge.cf_account_id,
                    namespace_id=edge.kv_namespace_id,
                    api_base=edge.api_base,
                    api_host_header=edge.api_host_header,
                ),
                system_domain=settings.system_default_domain,
            )
        runtime.meta_validator = MetaImageValidator(
            UrlRepository(db["urlsV2"]),
            UrlCache(cache_redis, ttl_seconds=settings.redis.redis_ttl_seconds),
            og_writethrough=og_writethrough,
            timeout=mt.fetch_timeout_seconds,
            max_bytes=mt.fetch_max_bytes,
            max_redirects=mt.fetch_max_redirects,
            user_agent=mt.fetch_user_agent,
        )
        log.info("meta_image_validator_registered", stream=mt.stream)

    if run_safety:
        # Safety analysis: own stream/group, same shape as meta-image.
        # The enforcer gets the worker's domain sink (link.blocked events
        # dispatch in-process when webhooks also run here) and an edge KV
        # client when the edge cache is configured.
        sf = settings.safety
        edge = settings.edge_cache
        if runtime.http_client is None:
            runtime.http_client = HttpClient(timeout=settings.http_client_timeout)
        safety_edge_kv = None
        if edge.enabled:
            safety_edge_kv = CloudflareKVClient(
                http_client=runtime.http_client,
                api_token=edge.cf_api_token,
                account_id=edge.cf_account_id,
                namespace_id=edge.kv_namespace_id,
                api_base=edge.api_base,
                api_host_header=edge.api_host_header,
            )
        safety_enforcer = SafetyEnforcer(
            UrlRepository(db["urlsV2"]),
            LegacyUrlRepository(db["urls"]),
            EmojiUrlRepository(db["emojis"]),
            UrlCache(cache_redis, ttl_seconds=settings.redis.redis_ttl_seconds),
            events=worker_domain_sink,
            edge_kv=safety_edge_kv,
            system_default_domain=settings.system_default_domain,
        )
        # Provider order mirrors the app wiring: operator sources, synced
        # feed sets (from FEED_REGISTRY), then online lookups last.
        worker_feed_repo = FeedDomainRepository(db["safety_feed_domains"])
        _, feed_analyzer, _ = build_feed_providers(sf, worker_feed_repo)
        safety_providers: list = [
            BlockedPatternProvider(
                BlockedUrlRepository(db["blocked-urls"]),
                regex_timeout=settings.blocked_url_regex_timeout,
            ),
            *feed_analyzer,
        ]
        if sf.web_risk_enabled:
            safety_providers.append(
                WebRiskProvider(
                    runtime.http_client,
                    api_key=sf.web_risk_api_key,
                    api_base=sf.web_risk_api_base,
                )
            )
        # Deep tier: unresolved screenings this worker judges get handed
        # to the admission policy and, when admitted, emitted onto the
        # investigation stream for its own consumer.
        deep_sink = None
        admission = None
        if sf.deep_enabled:
            deep_redis = await create_redis_client(
                ce.queue_redis_uri, label="safety-deep"
            )
            if deep_redis is not None:
                runtime.deep_redis = deep_redis
                deep_sink = RedisStreamDeepAnalysisSink(
                    deep_redis, stream=sf.deep_stream, maxlen=sf.deep_maxlen
                )
                admission = AdmissionPolicy(
                    deep_redis,
                    daily_budget=sf.deep_daily_budget,
                    report_daily_budget=sf.deep_report_daily_budget,
                    admit_sweeps=sf.deep_admit_sweeps,
                )
                log.info("safety_deep_sink_enabled", stream=sf.deep_stream)
        safety_analyzer = SafetyAnalyzer(
            safety_providers,
            VerdictRepository(db["safety_verdicts"]),
            safety_enforcer,
            DiscordOpsNotifier(
                settings.contact_webhook,
                settings.url_report_webhook,
                runtime.http_client,
            ),
            reverdict_ttl_hours=sf.reverdict_ttl_hours,
            admission=admission,
            deep_sink=deep_sink,
        )
        runtime.safety_consumer = SafetyAnalysisConsumer(safety_analyzer)
        # Sweeps emitted from THIS process run inline through the worker's
        # own analyzer — screenings are milliseconds, and it avoids a
        # second raw queue-redis client just to re-enter our own stream.
        worker_safety_sink = InlineSafetySink(safety_analyzer)
        log.info("safety_worker_registered", stream=sf.stream)

        # Only when this process also runs the hotness group — the actions
        # list is shared by reference with the already-built detector.
        if "hotness" in groups and runtime.consumers.get("hotness") is not None:
            actions.append(
                HotLinkScreen(
                    UrlRepository(db["urlsV2"]),
                    LegacyUrlRepository(db["urls"]),
                    VerdictRepository(db["safety_verdicts"]),
                    worker_safety_sink,
                    system_default_domain=settings.system_default_domain,
                )
            )
            log.info("safety_hot_screen_registered")

        # Deep tier consumer: its own stream, own consumer beside the
        # render sandbox. Requires both the deep queue AND the LLM
        # capability; without either it stays off and admitted events
        # simply queue with no reader (harmless — maxlen-trimmed).
        llm = settings.llm
        if sf.deep_enabled and deep_sink is not None and llm.enabled:
            llm_runner = LlmTaskRunner(llm)
            investigation_tools = build_investigation_tools(
                InvestigationToolDeps(
                    browser=BrowserRunClient(
                        runtime.http_client,
                        account_id=edge.cf_account_id,
                        api_token=edge.cf_api_token,
                    ),
                    http=runtime.http_client,
                    feed_repo=worker_feed_repo,
                    url_repo=UrlRepository(db["urlsV2"]),
                    own_domains=settings.blocked_self_domains,
                    web_risk=(
                        WebRiskProvider(
                            runtime.http_client,
                            api_key=sf.web_risk_api_key,
                            api_base=sf.web_risk_api_base,
                        )
                        if sf.web_risk_enabled
                        else None
                    ),
                )
            )
            investigate_task = build_investigate_task(
                prompt_dir=llm.prompt_dir, tools=investigation_tools
            )
            investigator = DeepInvestigator(
                llm_runner,
                investigate_task,
                UrlRepository(db["urlsV2"]),
                VerdictRepository(db["safety_verdicts"]),
                safety_enforcer,
                DiscordOpsNotifier(
                    settings.contact_webhook,
                    settings.url_report_webhook,
                    runtime.http_client,
                ),
                policy=AutoBlockPolicy(sf.deep_autoblock),
                model_name=llm.model,
            )
            runtime.deep_consumer = DeepAnalysisConsumer(investigator)
            log.info("safety_deep_worker_registered", stream=sf.deep_stream)
        elif sf.deep_enabled and deep_sink is not None:
            log.warning(
                "safety_deep_investigator_unconfigured",
                detail="deep investigation needs LLM_ENABLED",
            )
    if run_scheduler:
        # Scheduled-task runner: Mongo-only claim loop, so it shares this
        # process without extra infra (same reasoning as the webhook
        # executor above). The lease makes an overlapping embedded runner
        # in the app process harmless during runtime transitions.
        sch = settings.scheduler
        if runtime.http_client is None:
            runtime.http_client = HttpClient(timeout=settings.http_client_timeout)
        # Safety catalogs: feed syncs (with the delta sweep when this
        # process also analyzes) plus the scheduled sweeps.
        scheduler_feed_repo = FeedDomainRepository(db["safety_feed_domains"])
        scheduler_url_repo = UrlRepository(db["urlsV2"])
        feature_tasks = []
        delta_sweeper = (
            FeedDeltaSweeper(scheduler_url_repo, worker_safety_sink)
            if worker_safety_sink is not None
            else None
        )
        feature_tasks.extend(
            build_feed_tasks(
                settings.safety,
                runtime.http_client,
                scheduler_feed_repo,
                delta_sweeper,
            )
        )
        if worker_safety_sink is not None:
            feature_tasks.extend(
                build_sweep_tasks(
                    settings.safety,
                    SweepDeps(
                        url_repo=scheduler_url_repo,
                        verdict_repo=VerdictRepository(db["safety_verdicts"]),
                        sink=worker_safety_sink,
                    ),
                )
            )
        scheduler = TaskScheduler(
            ScheduledTaskRepository(db["scheduled_tasks"]),
            build_task_registry(feature_tasks),
            poll_interval=sch.poll_seconds,
            lease_seconds=sch.lease_seconds,
        )
        runtime.telemetry_tasks.append(
            asyncio.create_task(scheduler.run(), name="task-scheduler")
        )
        log.info("task_scheduler_worker_registered")

    # Telemetry: periodic backlog/lag stats (the Axiom alert signal) and
    # cleanup of restart-leftover consumer names. Best-effort — a missing
    # telemetry connection never blocks consumption.
    telemetry_redis = await create_redis_client(
        ce.queue_redis_uri, label="worker-telemetry"
    )
    if telemetry_redis is not None:
        runtime.telemetry_redis = telemetry_redis
        # extend, never assign — the webhooks executor task is already in
        # this list and an assignment would orphan it (uncancellable at
        # shutdown).
        runtime.telemetry_tasks.extend(
            [
                asyncio.create_task(
                    StreamMetricsReporter(
                        telemetry_redis, ce.stream, ce.stats_interval_seconds
                    ).run_forever()
                ),
                asyncio.create_task(
                    StaleConsumerJanitor(telemetry_redis, ce.stream).run_forever()
                ),
            ]
        )

    return runtime


def _register_group(
    broker: RedisBroker,
    ce: ClickEventsSettings,
    group: str,
    consumer_suffix: str,
    consumer_for: Any,
) -> None:
    """Register the reader + claimer subscriber pair for one group."""
    guard = ClaimDeadLetterGuard(
        stream=ce.stream,
        group=group,
        dlq_stream=ce.dlq_stream,
        max_deliveries=ce.max_deliveries,
    )

    async def reader(body: Any) -> None:
        await consumer_for(group).consume(body)

    reader.__name__ = f"{group}_reader"
    broker.subscriber(
        stream=StreamSub(
            ce.stream,
            group=group,
            consumer=f"{group}-{consumer_suffix}",
            max_records=ce.batch_size,
            polling_interval=ce.block_ms,
        )
    )(reader)

    async def claimer(body: Any, msg: RedisMessage, redis: Redis) -> None:
        message_id = _first_message_id(msg)
        if message_id and await guard.intercept(redis, message_id, body):
            return  # dead-lettered; normal return lets FastStream XACK
        await consumer_for(group).consume(body)

    claimer.__name__ = f"{group}_claimer"
    broker.subscriber(
        stream=StreamSub(
            ce.stream,
            group=group,
            consumer=f"{group}-{consumer_suffix}-claim",
            min_idle_time=ce.claim_idle_ms,
            polling_interval=ce.block_ms,
        )
    )(claimer)


def _register_safety(
    broker: RedisBroker,
    sf: Any,
    consumer_suffix: str,
    safety_consumer_for: Any,
) -> None:
    """Reader + claimer pair for the safety analysis stream."""
    guard = ClaimDeadLetterGuard(
        stream=sf.stream,
        group="safety",
        dlq_stream=sf.dlq_stream,
        max_deliveries=sf.max_deliveries,
    )

    async def reader(body: Any) -> None:
        await safety_consumer_for().consume(body)

    reader.__name__ = "safety_reader"
    broker.subscriber(
        stream=StreamSub(
            sf.stream,
            group="safety",
            consumer=f"safety-{consumer_suffix}",
            max_records=sf.batch_size,
            polling_interval=sf.block_ms,
        )
    )(reader)

    async def claimer(body: Any, msg: RedisMessage, redis: Redis) -> None:
        message_id = _first_message_id(msg)
        if message_id and await guard.intercept(redis, message_id, body):
            return
        await safety_consumer_for().consume(body)

    claimer.__name__ = "safety_claimer"
    broker.subscriber(
        stream=StreamSub(
            sf.stream,
            group="safety",
            consumer=f"safety-{consumer_suffix}-claim",
            min_idle_time=sf.claim_idle_ms,
            polling_interval=sf.block_ms,
        )
    )(claimer)


def _register_safety_deep(
    broker: RedisBroker,
    sf: Any,
    consumer_suffix: str,
    deep_consumer_for: Any,
) -> None:
    """Reader + claimer pair for the investigation stream — its own group
    and DLQ, so a sweep flood on the screening stream can never delay a
    reported host here (the whole reason the deep queue is separate)."""
    guard = ClaimDeadLetterGuard(
        stream=sf.deep_stream,
        group="safety-deep",
        dlq_stream=sf.deep_dlq_stream,
        max_deliveries=sf.deep_max_deliveries,
    )

    async def reader(body: Any) -> None:
        await deep_consumer_for().consume(body)

    reader.__name__ = "safety_deep_reader"
    broker.subscriber(
        stream=StreamSub(
            sf.deep_stream,
            group="safety-deep",
            consumer=f"safety-deep-{consumer_suffix}",
            max_records=sf.deep_batch_size,
            polling_interval=sf.deep_block_ms,
        )
    )(reader)

    async def claimer(body: Any, msg: RedisMessage, redis: Redis) -> None:
        message_id = _first_message_id(msg)
        if message_id and await guard.intercept(redis, message_id, body):
            return
        await deep_consumer_for().consume(body)

    claimer.__name__ = "safety_deep_claimer"
    broker.subscriber(
        stream=StreamSub(
            sf.deep_stream,
            group="safety-deep",
            consumer=f"safety-deep-{consumer_suffix}-claim",
            min_idle_time=sf.deep_claim_idle_ms,
            polling_interval=sf.deep_block_ms,
        )
    )(claimer)


def _register_meta_image(
    broker: RedisBroker,
    mt: Any,
    consumer_suffix: str,
    validator_for: Any,
) -> None:
    """Reader + claimer pair for the meta-image validation stream."""
    guard = ClaimDeadLetterGuard(
        stream=mt.stream,
        group="meta-image",
        dlq_stream=mt.dlq_stream,
        max_deliveries=mt.max_deliveries,
    )

    async def reader(body: Any) -> None:
        await validator_for().consume(body)

    reader.__name__ = "meta_image_reader"
    broker.subscriber(
        stream=StreamSub(
            mt.stream,
            group="meta-image",
            consumer=f"meta-image-{consumer_suffix}",
            max_records=mt.batch_size,
            polling_interval=mt.block_ms,
        )
    )(reader)

    async def claimer(body: Any, msg: RedisMessage, redis: Redis) -> None:
        message_id = _first_message_id(msg)
        if message_id and await guard.intercept(redis, message_id, body):
            return
        await validator_for().consume(body)

    claimer.__name__ = "meta_image_claimer"
    broker.subscriber(
        stream=StreamSub(
            mt.stream,
            group="meta-image",
            consumer=f"meta-image-{consumer_suffix}-claim",
            min_idle_time=mt.claim_idle_ms,
            polling_interval=mt.block_ms,
        )
    )(claimer)


def _register_domain_webhooks(
    broker: RedisBroker,
    ce: ClickEventsSettings,
    consumer_suffix: str,
    domain_consumer_for: Any,
) -> None:
    """Reader + claimer pair for the ``webhooks`` group on events:domain.

    Reuses the click pipeline's batch/claim tunables — domain events are a
    tiny fraction of click volume, so dedicated knobs would be dead config.
    """
    guard = ClaimDeadLetterGuard(
        stream=DOMAIN_EVENTS_STREAM,
        group="webhooks",
        dlq_stream=f"{DOMAIN_EVENTS_STREAM}:dlq",
        max_deliveries=ce.max_deliveries,
    )

    async def reader(body: Any) -> None:
        await domain_consumer_for().consume(body)

    reader.__name__ = "webhooks_domain_reader"
    broker.subscriber(
        stream=StreamSub(
            DOMAIN_EVENTS_STREAM,
            group="webhooks",
            consumer=f"webhooks-{consumer_suffix}",
            max_records=ce.batch_size,
            polling_interval=ce.block_ms,
        )
    )(reader)

    async def claimer(body: Any, msg: RedisMessage, redis: Redis) -> None:
        message_id = _first_message_id(msg)
        if message_id and await guard.intercept(redis, message_id, body):
            return
        await domain_consumer_for().consume(body)

    claimer.__name__ = "webhooks_domain_claimer"
    broker.subscriber(
        stream=StreamSub(
            DOMAIN_EVENTS_STREAM,
            group="webhooks",
            consumer=f"webhooks-{consumer_suffix}-claim",
            min_idle_time=ce.claim_idle_ms,
            polling_interval=ce.block_ms,
        )
    )(claimer)


def _first_message_id(msg: Any) -> str | None:
    ids = (getattr(msg, "raw_message", None) or {}).get("message_ids") or []
    if not ids:
        return None
    first = ids[0]
    return first.decode() if isinstance(first, bytes) else str(first)


def create_worker_app(settings: AppSettings | None = None) -> AsgiFastStream:
    """Build the worker application (separated from ``create_app`` for tests)."""
    if settings is None:
        settings = AppSettings()
    ce = settings.click_events

    mt = settings.meta_tags
    wh = settings.webhooks
    run_clicks = ce.sink == "stream" and bool(ce.queue_redis_uri)
    run_meta = mt.async_image_validation and bool(ce.queue_redis_uri)
    run_webhooks = (
        wh.enabled and wh.runtime in ("auto", "worker") and bool(ce.queue_redis_uri)
    )
    # Safety analysis rides its own stream; with queue Redis absent the app
    # analyzes inline and the worker has nothing to consume.
    run_safety = settings.safety.enabled and bool(ce.queue_redis_uri)
    # The investigation consumer registers only when the deep tier AND the
    # LLM capability are both on; the deep runtime build (which needs the
    # http client + feed repo) short-circuits the investigator otherwise.
    run_safety_deep = (
        run_safety and settings.safety.deep_enabled and settings.llm.enabled
    )
    # The scheduler rides along when the worker runs for other reasons; it
    # never justifies booting a worker by itself (auto resolves to embedded
    # in worker-less deploys — see SchedulerSettings).
    run_scheduler = settings.scheduler.enabled and settings.scheduler.runtime in (
        "auto",
        "worker",
    )
    if not run_clicks and not run_meta and not run_webhooks and not run_safety:
        raise RuntimeError(
            "The worker requires CLICK_EVENTS_QUEUE_REDIS_URI plus at least "
            "one of CLICK_EVENTS_SINK=stream, META_TAGS_ASYNC_IMAGE_VALIDATION, "
            "WEBHOOKS_ENABLED, or SAFETY_ENABLED. Refusing to start so a "
            "misconfigured deployment fails loudly instead of idling."
        )

    groups = enabled_groups(ce) if run_clicks else []
    if run_clicks and not groups:
        raise RuntimeError(
            "No consumer groups enabled for this worker — check "
            "CLICK_EVENTS_WORKER_GROUPS and CLICK_EVENTS_HOTNESS_ENABLED."
        )

    broker = RedisBroker(ce.queue_redis_uri)
    runtime_holder: dict[str, _WorkerRuntime] = {}

    def consumer_for(group: str) -> ClickConsumer:
        runtime = runtime_holder.get("runtime")
        if runtime is None:  # pragma: no cover — startup hook always runs first
            raise RuntimeError("worker runtime not initialised")
        return runtime.consumers[group]

    def validator_for() -> MetaImageValidator:
        runtime = runtime_holder.get("runtime")
        if runtime is None or runtime.meta_validator is None:  # pragma: no cover
            raise RuntimeError("worker runtime not initialised")
        return runtime.meta_validator

    def domain_consumer_for() -> WebhookDomainConsumer:
        runtime = runtime_holder.get("runtime")
        if (
            runtime is None or runtime.webhook_domain_consumer is None
        ):  # pragma: no cover
            raise RuntimeError("worker runtime not initialised")
        return runtime.webhook_domain_consumer

    def safety_consumer_for() -> SafetyAnalysisConsumer:
        runtime = runtime_holder.get("runtime")
        if runtime is None or runtime.safety_consumer is None:  # pragma: no cover
            raise RuntimeError("worker runtime not initialised")
        return runtime.safety_consumer

    def deep_consumer_for() -> DeepAnalysisConsumer:
        runtime = runtime_holder.get("runtime")
        if runtime is None or runtime.deep_consumer is None:  # pragma: no cover
            raise RuntimeError("worker runtime not initialised")
        return runtime.deep_consumer

    consumer_suffix = f"{socket.gethostname()}-{os.getpid()}"
    for group in groups:
        _register_group(broker, ce, group, consumer_suffix, consumer_for)
    if run_meta:
        _register_meta_image(broker, mt, consumer_suffix, validator_for)
    if run_webhooks:
        # link.clicked feed: the `webhooks` group is a sibling of stats/
        # hotness on the click stream (only when clicks ride the stream).
        if run_clicks:
            _register_group(broker, ce, "webhooks", consumer_suffix, consumer_for)
        _register_domain_webhooks(broker, ce, consumer_suffix, domain_consumer_for)
    if run_safety:
        _register_safety(broker, settings.safety, consumer_suffix, safety_consumer_for)
    if run_safety_deep:
        _register_safety_deep(
            broker, settings.safety, consumer_suffix, deep_consumer_for
        )

    async def _startup() -> None:
        runtime_holder["runtime"] = await _build_runtime(
            settings,
            groups,
            run_meta=run_meta,
            run_webhooks=run_webhooks,
            run_safety=run_safety,
            run_scheduler=run_scheduler,
        )
        log.info(
            "click_worker_started",
            stream=ce.stream,
            groups=groups + (["webhooks"] if run_webhooks else []),
            claim_idle_ms=ce.claim_idle_ms,
            max_deliveries=ce.max_deliveries,
        )

    async def _shutdown() -> None:
        runtime = runtime_holder.pop("runtime", None)
        if runtime is not None:
            await runtime.aclose()
        log.info("click_worker_stopped")

    return AsgiFastStream(
        broker,
        asgi_routes=[("/health", make_ping_asgi(broker, timeout=5.0))],
        on_startup=[_startup],
        on_shutdown=[_shutdown],
    )


def create_app() -> AsgiFastStream:
    """uvicorn --factory entrypoint.

    ``load_dotenv`` runs here (not at import time) so importing this module
    never mutates ``os.environ`` — pydantic-settings reads the .env file on
    its own; this call only covers non-settings ``os.environ`` readers, the
    same contract as ``main.py``.
    """
    load_dotenv()
    setup_logging()
    return create_worker_app()
