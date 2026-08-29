"""
Application configuration via pydantic-settings.

All settings are loaded from environment variables (and .env file).

Decision on secret key env var: kept as SECRET_KEY for the new app, but
FLASK_SECRET_KEY is also accepted as an alias for backward compatibility
(handled in AppSettings via model_validator).
"""

from __future__ import annotations

from functools import cached_property
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    mongodb_uri: str
    db_name: str = "url-shortener"
    max_pool_size: int = 200
    min_pool_size: int = 10


class RedisSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Optional — self-hosters without Redis get degraded-but-functional behaviour
    redis_uri: str | None = None
    redis_ttl_seconds: int = 3600

    # Feature flag cache TTLs. Positive results cached for `feature_flag_ttl_seconds`
    # so flag flips propagate within that window. Negative results (unregistered
    # flag names) cached for a shorter window so newly-registered flags become
    # visible faster than the positive TTL.
    feature_flag_ttl_seconds: int = 60
    feature_flag_negative_ttl_seconds: int = 30


class JWTSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    jwt_issuer: str = "spoo.me"
    jwt_audience: str = "spoo.me.api"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 2592000
    cookie_secure: bool = True

    # RS256 keys (preferred)
    jwt_private_key: str = ""
    jwt_public_key: str = ""

    # HS256 fallback (used when RS256 keys are absent)
    jwt_secret: str = ""

    @property
    def use_rs256(self) -> bool:
        return bool(self.jwt_private_key and self.jwt_public_key)


class OAuthProviderSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_redirect_uri: str = ""

    github_oauth_client_id: str = ""
    github_oauth_client_secret: str = ""
    github_oauth_redirect_uri: str = ""

    discord_oauth_client_id: str = ""
    discord_oauth_client_secret: str = ""
    discord_oauth_redirect_uri: str = ""


class EmailSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    zepto_api_token: str = ""
    zepto_from_email: str = "noreply@spoo.me"
    zepto_from_name: str = "spoo.me"


class LoggingSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    log_level: str = "INFO"
    log_format: str = "console"  # "json" in production

    # Sampling rates (0.0-1.0)
    sample_rate_redirect: float = 0.05
    sample_rate_stats: float = 0.20
    sample_rate_cache: float = 0.01
    sample_rate_export: float = 0.80


class SentrySettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    sentry_dsn: str = ""
    sentry_send_pii: bool = False
    sentry_traces_sample_rate: float = 0.1
    sentry_profile_sample_rate: float = 0.05

    @property
    def client_key(self) -> str:
        """Extract the public key from the DSN for the frontend loader script."""
        if not self.sentry_dsn:
            return ""
        try:
            return urlparse(self.sentry_dsn).username or ""
        except Exception:
            return ""


class CustomDomainSettings(BaseSettings):
    """User-bring-your-own-domain feature config.

    All fields default to safe values that keep the feature off until the
    rollout flag flips. ``enabled`` is the master switch consulted by the
    service layer (PR5+); the data plumbing (schema, repo, wiring) lands
    even when False so the rollout has a clean code path to flip.

    All env vars must be prefixed ``CUSTOM_DOMAINS_`` so generic names
    like ``ENABLED`` or ``MAX_PER_USER`` set elsewhere in the deploy
    environment don't accidentally configure this feature.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        env_prefix="CUSTOM_DOMAINS_",
    )

    # Master switch consulted by CustomDomainService. Until True, every
    # public method short-circuits with FeatureDisabledError.
    enabled: bool = False

    # Local-dev escape hatch: wire MockDcvBackend instead of Cloudflare.
    # register() returns the same two-record shape prod serves (routing
    # CNAME + ownership TXT) and verify() always succeeds, so the full
    # PENDING → ACTIVE dashboard flow works without CF creds. Guarded:
    # startup fails if enabled while ENV=production (see model_validator).
    mock_dcv: bool = False

    # Quotas. Flat for all users in v1 (no tier branching).
    # All counts must be >= 1 — a zero quota silently bricks the feature
    # (every create raises QuotaExceeded with no log signal that the cause
    # is config, not abuse). Validators below fail container startup instead.
    max_per_user: int = Field(default=1, ge=1)
    # Generous because CF's own DCV cadence can take 5-15 min per probe;
    # legitimate users may poll many times during initial activation.
    verify_attempts_per_hour: int = Field(default=60, ge=1)

    # Background re-verify worker tunables.
    # interval=0 would busy-loop the worker; batch=0 wastes a tick;
    # max_age<=0 would suspend every active domain on first tick.
    reverify_interval_seconds: int = Field(default=3600, ge=1)
    reverify_batch_size: int = Field(default=10, ge=1)
    max_verify_age_seconds: int = Field(default=7 * 24 * 3600, ge=1)
    suspend_after_consecutive_failures: int = Field(default=3, ge=1)

    # Cloudflare for SaaS Custom Hostnames. Both required when `enabled=True` —
    # the service raises FeatureDisabledError when missing so OSS forks without
    # CF still boot cleanly.
    cf_zone_id: str | None = None
    cf_api_token: str | None = None
    # CNAME target customers point their hostnames at. Proxied (orange-cloud)
    # in the spoo.me zone; CF SaaS dispatches matched traffic to the zone
    # fallback_origin (proxy-fallback.spoo.me) which lands on Caddy :443.
    cf_cname_target: str = "customers.spoo.me"
    # Per-zone delegation hostname for Delegated DCV (optional path; HTTP DCV
    # is the default). When set, customers using cf_delegated_dcv add
    # `_acme-challenge.<fqdn> CNAME <fqdn>.<this value>` so CF auto-renews
    # without re-probing. Empty disables the second-CNAME instruction.
    cf_dcv_delegation_target: str = ""
    # Retry policy for CF API calls. Three attempts with exponential backoff.
    cf_api_max_retries: int = Field(default=3, ge=1)
    cf_api_initial_backoff_seconds: float = Field(default=1.0, gt=0)

    @model_validator(mode="after")
    def _validate_cf_saas_config(self) -> CustomDomainSettings:
        if self.cf_zone_id and not self.cf_api_token:
            raise ValueError(
                "CF SaaS path requires cf_api_token when cf_zone_id is set."
            )
        return self


# Consumer groups the click worker knows how to run. Adding a new consumer =
# implement it in workers/click_worker.py and register its name here.
CLICK_WORKER_GROUPS = ("stats", "hotness")


class ClickEventsSettings(BaseSettings):
    """Click event pipeline config (lean redirect path + fanout consumers).

    Defaults keep the feature fully off: ``sink="inline"`` preserves the
    classic synchronous tracking byte-for-byte — no stream, no queue Redis,
    no worker required. Stream mode additionally requires
    ``queue_redis_uri`` pointing at a SEPARATE Redis instance from the URL
    cache: the cache runs ``allkeys-lru`` and would silently evict stream
    entries (= click loss); the queue Redis must run ``noeviction`` + AOF.

    All env vars are prefixed ``CLICK_EVENTS_`` so generic names set
    elsewhere in the deploy environment don't accidentally configure this
    feature (same convention as ``CUSTOM_DOMAINS_``).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        env_prefix="CLICK_EVENTS_",
    )

    # Master switch: "inline" = classic synchronous tracking (default),
    # "stream" = emit to the Redis Stream and let the worker consume.
    sink: Literal["inline", "stream"] = "inline"
    queue_redis_uri: str = ""

    stream: str = "events:clicks"
    dlq_stream: str = "events:clicks:dlq"
    # Acked-history tail kept in the stream (XADD ACKED MAXLEN ~, Redis 8.2+):
    # every publish sweeps entries beyond this that ALL groups have acked, so
    # consumed history self-cleans server-side (~7MB at the default). Unacked
    # backlog is never trimmed regardless of size — its only bound is the
    # queue Redis noeviction memory cap (backpressure → inline fallback).
    maxlen: int = Field(default=10_000, ge=1000)

    # Consumer tunables.
    batch_size: int = Field(default=100, ge=1)
    block_ms: int = Field(default=2000, ge=100)
    # Pending messages idle longer than claim_idle_ms are reclaimed by the
    # claimer subscriber (XAUTOCLAIM); after max_deliveries failed attempts
    # a message is dead-lettered to dlq_stream.
    claim_idle_ms: int = Field(default=60_000, ge=1000)
    max_deliveries: int = Field(default=5, ge=1)
    stats_interval_seconds: float = Field(default=30.0, gt=0)

    # Which consumer groups this worker process runs. Lets a future
    # deployment split groups across containers without code changes.
    worker_groups: list[str] = Field(default=list(CLICK_WORKER_GROUPS))

    # Hot-URL detection (fixed window counters; threshold mirrors the
    # cf-edge plan's 50 hits / 60s promotion rule).
    hotness_enabled: bool = False
    hot_threshold: int = Field(default=50, ge=2)
    hot_window_seconds: int = Field(default=60, ge=10)

    @field_validator("worker_groups")
    @classmethod
    def _known_groups_only(cls, v: list[str]) -> list[str]:
        unknown = set(v) - set(CLICK_WORKER_GROUPS)
        if unknown:
            raise ValueError(
                f"Unknown click worker group(s): {sorted(unknown)}. "
                f"Known groups: {list(CLICK_WORKER_GROUPS)}"
            )
        if not v:
            raise ValueError("worker_groups must not be empty")
        return v


class EdgeCacheSettings(BaseSettings):
    """CF edge cache for hot URLs — a deployment optimization, never a
    product requirement (design: thoughts/cf-edge-cache-v2.md).

    Fully off unless ALL THREE Cloudflare fields are set. Promotion rides
    the hotness consumer, so stream mode + hotness are prerequisites.
    Invalidation in v1 is TTL-only: entries self-expire, and
    ``wrangler kv key delete`` is the manual purge / abuse-takedown lever.

    Env vars prefixed ``EDGE_CACHE_`` (same convention as
    ``CUSTOM_DOMAINS_`` / ``CLICK_EVENTS_``).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        env_prefix="EDGE_CACHE_",
    )

    # The Workers KV REST API is account-scoped:
    # /accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/{key}
    cf_account_id: str | None = None
    cf_api_token: str | None = None  # Workers KV write scope only
    kv_namespace_id: str | None = None
    # Local-dev emulator override: point at wrangler dev's Explorer API
    # (http://host.docker.internal:8787/cdn-cgi/explorer/api), which
    # mirrors the same /storage/kv/... paths. Unset in real deployments —
    # the production URL derives from cf_account_id.
    api_base: str | None = None
    # Dev-only companion to api_base: wrangler dev validates the Host
    # header (localhost forms only), so containers reaching it through
    # host.docker.internal must present "localhost:8787".
    api_host_header: str | None = None

    # Entry lifetime at the edge. Deliberately short: with TTL-only
    # invalidation this IS the worst-case staleness bound after a URL
    # changes. Raising it trades origin load for staleness.
    ttl_seconds: int = Field(default=300, ge=60)
    # ±jitter so simultaneously-promoted URLs don't expire in lockstep
    # and stampede origin together.
    ttl_jitter_ratio: float = Field(default=0.2, ge=0.0, le=0.5)
    # og_only write-through TTL — much longer (entries are event-managed,
    # not hot-promoted), but bounded so a missed delete or out-of-band
    # block can't keep serving a stale card forever.
    og_ttl_seconds: int = Field(default=86_400, ge=60)

    @property
    def enabled(self) -> bool:
        return bool(
            self.cf_api_token
            and self.kv_namespace_id
            and (self.cf_account_id or self.api_base)
        )


class R2StorageSettings(BaseSettings):
    """Cloudflare R2 bucket for user-uploaded og:images (custom meta-tags).

    Fully off unless all five fields are set — self-hosts without R2 keep
    https image URLs working; only data-URI uploads are rejected. Env vars
    prefixed ``R2_`` (same convention as ``EDGE_CACHE_``).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        env_prefix="R2_",
    )

    account_id: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    bucket: str | None = None
    # The bucket's public/custom domain, e.g. https://og.spoo.me or the
    # r2.dev URL — becomes the stored og:image URL prefix.
    public_base_url: str | None = None
    # Local-dev S3 emulator override (MinIO etc.); unset in deployments.
    endpoint_url: str | None = None
    # Image PUTs need more headroom than the shared client's 5s default.
    request_timeout_seconds: float = Field(default=15.0, gt=0)
    # Decoded data-URI cap. NOTE: base64 inflates 4/3 and the app-wide
    # MAX_CONTENT_LENGTH is 1MB — raising this needs raising that too.
    # 512KB decoded ≈ 683KB on the wire; WhatsApp reliability wants ≤300KB
    # anyway (surfaced as an API warning above that).
    upload_max_bytes: int = Field(default=512_000, ge=1024)

    @property
    def enabled(self) -> bool:
        return bool(
            self.account_id
            and self.access_key_id
            and self.secret_access_key
            and self.bucket
            and self.public_base_url
        )


class MetaTagsSettings(BaseSettings):
    """Async og:image validation for custom meta-tags.

    Rides the SAME queue Redis as click events (CLICK_EVENTS_QUEUE_REDIS_URI)
    — one durable Redis per deploy. Effective only when that queue is
    configured; otherwise validation silently skips (the synchronous checks
    at write time already ran). Env vars prefixed ``META_TAGS_``.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        env_prefix="META_TAGS_",
    )

    async_image_validation: bool = True
    stream: str = "events:meta-image"
    dlq_stream: str = "events:meta-image:dlq"
    maxlen: int = Field(default=1_000, ge=100)
    batch_size: int = Field(default=10, ge=1)
    block_ms: int = Field(default=2000, ge=100)
    # Click defaults (60s) are tuned for millisecond handlers; a batch of
    # 10 fetches x 5s timeout can legitimately run ~50s, and a claimer
    # stealing from a live reader means duplicate fetches. The CAS repo
    # filter makes duplicates harmless anyway — this just avoids the waste.
    claim_idle_ms: int = Field(default=120_000, ge=1000)
    max_deliveries: int = Field(default=3, ge=1)

    fetch_timeout_seconds: float = Field(default=5.0, gt=0)
    fetch_max_bytes: int = Field(default=1_048_576, ge=1024)
    fetch_max_redirects: int = Field(default=3, ge=0)
    # Sent on og:image validation and /api/v1/metadata fetches. Transparent
    # by design (self-hosters should brand their own); some WAFs 401/403
    # unknown UAs — safe_fetch classifies that as denied, never data loss.
    fetch_user_agent: str = "spoo.me-og-validator/1.0 (+https://spoo.me)"


class WebRiskSettings(BaseSettings):
    """Google Web Risk lookups for the URL expander's safety verdict.

    Env vars are prefixed ``WEB_RISK_`` so a generic ``API_KEY`` set
    elsewhere in the deploy environment can't configure this. Without a
    key the check silently doesn't run and the verdict is absent — the
    tool never shows a fabricated one.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        env_prefix="WEB_RISK_",
    )

    api_key: str = ""
    timeout_seconds: float = Field(default=4.0, gt=0)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


class WebhookSettings(BaseSettings):
    """Webhooks system — real-time event deliveries to subscriber URLs.

    Fully opt-in: ``enabled=False`` wires the NullSink and mounts nothing.
    Fact transport rides the click pipeline's queue Redis when present and
    degrades to inline dispatch without it; the delivery executor needs
    only Mongo, so ``runtime`` chooses where it lives:

      auto     — worker when the worker process mounts it, else embedded
      worker   — only the click worker runs consumers + executor
      embedded — the app process runs them as lifespan tasks
      off      — dispatch still records nothing is running (dev/debug)
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        env_prefix="WEBHOOKS_",
    )

    enabled: bool = False
    runtime: Literal["auto", "worker", "embedded", "off"] = "auto"

    max_endpoints: int = Field(default=5, ge=1)
    delivery_timeout_seconds: float = Field(default=15.0, gt=0)
    max_payload_bytes: int = Field(default=20_480, ge=1024)
    max_consecutive_failures: int = Field(default=10, ge=1)
    # Governs BOTH webhook-events and webhook-deliveries TTL indexes — a
    # delivery must never outlive its event.
    delivery_log_ttl_days: int = Field(default=30, ge=1)
    # Dispatch-side pending cap per endpoint; beyond it deliveries are
    # dropped-and-counted (surfaced as dropped_since_last).
    max_pending_per_endpoint: int = Field(default=1000, ge=10)
    executor_poll_seconds: float = Field(default=1.0, gt=0)
    executor_lease_seconds: int = Field(default=60, ge=10)
    # Owner→subscription-count cache in front of the matcher.
    matcher_cache_ttl_seconds: int = Field(default=60, ge=5)
    domain_stream_maxlen: int = Field(default=100_000, ge=1000)

    @property
    def delivery_log_ttl_seconds(self) -> int:
        return self.delivery_log_ttl_days * 86_400


class LlmSettings(BaseSettings):
    """The LLM capability — one client, many consumers.

    This is deliberately NOT a safety setting: the model is
    infrastructure (``infrastructure/llm``), registered tasks are the
    consumers (safety investigation first, the report-inbox scanner
    next), and the plumbing — client, retries, budgets, cost accounting —
    is owned once here. ``enabled`` is the kill switch: off means every
    task runner returns a typed failure instead of calling out.

    The model is a config value on purpose: every task result records the
    model + prompt version it was produced by, so swapping providers is a
    replay-and-compare exercise, not a leap of faith.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        env_prefix="LLM_",
    )

    enabled: bool = False
    # ``<provider>:<model>``; anthropic and openai are both wired. The
    # model is a config value on purpose — see the class docstring.
    model: str = "anthropic:claude-sonnet-5"
    api_key: str = ""
    # Hard ceilings applied to every task unless the task declares tighter
    # ones. Requests = model round-trips in one task run (tool loop
    # included); tokens = total across the run.
    # Measured: 8 cut off 10 of 167 eval investigations mid-flight.
    max_requests_per_run: int = Field(default=12, ge=1)
    # Measured: a clean investigation uses 4-5 tool calls; one whose
    # destination is dead legitimately explores more (root, variants,
    # chain) before concluding. 10 cut those off mid-investigation.
    max_tool_calls_per_run: int = Field(default=18, ge=1)
    max_total_tokens_per_run: int = Field(default=120_000, ge=1000)
    request_timeout_seconds: float = Field(default=60.0, gt=0)
    run_timeout_seconds: float = Field(default=300.0, gt=0)
    # Prompt overrides: a directory of <task>.md files that replaces the
    # in-repo defaults — production prompt text is private tuning.
    prompt_dir: str = ""


class SafetySettings(BaseSettings):
    """URL safety pipeline — report-triggered destination analysis,
    verdicts, and automatic enforcement.

    Fully opt-in: ``enabled=False`` wires the Null sink and nothing
    analyzes. With queue Redis present, analysis requests ride the
    ``events:safety`` stream and the click worker consumes; without it,
    analysis runs inline in the app process. Detection thresholds and
    weights are environment-only ON PURPOSE — they are private tuning,
    never committed.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        env_prefix="SAFETY_",
    )

    enabled: bool = False
    stream: str = "events:safety"
    dlq_stream: str = "events:safety:dlq"
    maxlen: int = Field(default=10_000, ge=1000)
    batch_size: int = Field(default=16, ge=1)
    block_ms: int = Field(default=5000, ge=100)
    # Must exceed a large host block, or the claimer re-delivers it mid-flight.
    claim_idle_ms: int = Field(default=600_000, ge=1000)
    max_deliveries: int = Field(default=5, ge=1)
    # A verdict younger than this short-circuits re-analysis of the same
    # destination host (repeat reports of one campaign are the norm).
    reverdict_ttl_hours: int = Field(default=24, ge=1)

    # L1 creation-pattern scoring (needs the queue Redis for counters).
    # Thresholds fire once per window on exact equality; production values
    # are PRIVATE tuning set via env, calibrated by replaying past
    # campaigns — the defaults here are deliberately conservative.
    # Each changes what the public create API refuses, so each has its own switch.
    manual_feed_enabled: bool = True
    shorteners_enabled: bool = False

    l1_enabled: bool = True
    l1_burst_window_seconds: int = Field(default=600, ge=60)
    # Replayed against 92 days of prod creates: campaigns peaked at 34/10min
    # while 50/300 caught none of twelve known ones.
    l1_domain_burst_threshold: int = Field(default=25, ge=2)
    l1_domain_daily_threshold: int = Field(default=150, ge=2)

    # L3 recent-screening sweep: every destination created in the window
    # gets screened by the cheap providers (silently) so nothing sits
    # permanently unjudged. The feed-delta sweep has no settings of its
    # own — it rides each feed's sync task.
    sweep_recent_enabled: bool = True
    sweep_recent_window_hours: int = Field(default=48, ge=1)
    sweep_max_enqueues: int = Field(default=1000, ge=10)

    # L2 investigation stage — its own queue and consumer, entered only
    # through the admission policy when screening ends unresolved.
    # Reports and destination edits always admit; pattern bursts admit
    # within the daily budget; sweep novelty never admits by default.
    deep_enabled: bool = False
    deep_stream: str = "events:safety:deep"
    deep_dlq_stream: str = "events:safety:deep:dlq"
    deep_maxlen: int = Field(default=5_000, ge=100)
    deep_batch_size: int = Field(default=4, ge=1)
    deep_block_ms: int = Field(default=5000, ge=100)
    # Must exceed LLM_RUN_TIMEOUT_SECONDS (300s) with headroom, or the claimer
    # re-claims runs that are still finishing — duplicate model spend.
    deep_claim_idle_ms: int = Field(default=900_000, ge=1000)
    deep_max_deliveries: int = Field(default=3, ge=1)
    deep_report_daily_budget: int = Field(default=200, ge=1)
    deep_daily_budget: int = Field(default=200, ge=1)
    deep_admit_sweeps: bool = False
    # Auto-block policy for a model toxic verdict:
    #   corroborated — a hard source (report, feed, Web Risk) must agree
    #                  (default; the model alone goes to review)
    #   confident    — a high-confidence model verdict blocks alone
    #   both         — high confidence AND corroboration
    #   off          — never auto-block; every toxic verdict is reviewed
    # Graduate from corroborated by measurement (review taps are labels),
    # not by trusting the model up front.
    deep_autoblock: Literal["corroborated", "confident", "both", "off"] = "corroborated"

    # External feeds. fishfish.gg is free/no-auth and defaults on (within
    # the master switch); its domain set syncs hourly via the scheduler.
    fishfish_enabled: bool = True
    fishfish_api_url: str = "https://api.fishfish.gg/v1/domains"
    # Google Web Risk Lookup API — enabled by setting the GCP API key.
    # (The Safe Browsing API is non-commercial-only; Web Risk is the
    # sanctioned equivalent.)
    web_risk_api_key: str = ""
    web_risk_api_base: str = "https://webrisk.googleapis.com"

    @property
    def web_risk_enabled(self) -> bool:
        return bool(self.web_risk_api_key)


class SchedulerSettings(BaseSettings):
    """Scheduled-task system — the Mongo-lease runner for recurring jobs.

    Mongo-only (the one dependency every deploy has), so ``runtime``
    chooses the host process with the same rule as webhooks:

      auto     — worker when a queue Redis is configured (a worker exists
                 in that deploy), else embedded in the app lifespan
      worker   — only the click worker runs the loop
      embedded — the app process runs it as a lifespan task
      off      — nothing polls; tasks stay armed in Mongo

    Enabled by default: with only built-in tasks registered it is a
    once-an-hour heartbeat, and feature tasks gate themselves on their own
    settings. The lease dedupes overlapping runners (deploy overlap,
    worker plus app) only while a run finishes within ``lease_seconds`` —
    the real guarantee is at-least-once with idempotent handlers, so a
    task expected to run longer than the lease needs the lease raised
    first (there is no mid-run renewal).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        env_prefix="SCHEDULER_",
    )

    enabled: bool = True
    runtime: Literal["auto", "worker", "embedded", "off"] = "auto"
    # Poll granularity bounds how late a task can start; 5s is invisible
    # for minute-level cron schedules and costs one indexed no-op query.
    poll_seconds: float = Field(default=5.0, gt=0)
    # A crashed run re-claims after this. Must exceed the slowest task;
    # tasks are idempotent by contract so an early expiry re-runs, never
    # corrupts.
    lease_seconds: int = Field(default=600, ge=30)


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Core
    secret_key: str = ""
    flask_secret_key: str = ""  # backward-compat alias
    app_version: str = "dev"  # injected by CI via the APP_VERSION build arg
    env: str = "development"
    app_url: str = "https://spoo.me"
    app_name: str = "spoo.me"

    # The Next frontend owns /onboarding; until the edge routes it there,
    # new OAuth accounts must land on the default redirect or they 404
    # right after signup. Flip ONBOARDING_REDIRECT_ENABLED=true at frontend
    # cutover; delete the flag once the old UI is retired.
    onboarding_redirect_enabled: bool = False

    # Our-deployment-only: intercepted HTML error statuses return empty
    # bodies + X-Error-Code so Caddy can compose the Next error page at the
    # edge. Flip EDGE_COMPOSED_ERRORS=true at frontend cutover; self-hosters
    # never set it and keep the branded error.html exactly as today.
    edge_composed_errors: bool = False

    @cached_property
    def system_default_domain(self) -> str:
        """Canonical fqdn for shorts created without an explicit custom domain."""
        # Hard fail rather than fall back — a wrong APP_URL silently
        # mis-attributes every short to the wrong host.
        parsed = urlparse(self.app_url)
        if not parsed.scheme or not parsed.hostname:
            raise ValueError(
                f"APP_URL is missing or invalid: {self.app_url!r}. "
                "Set APP_URL to your shortener's public URL (e.g. https://spoo.me)."
            )
        return parsed.hostname.lower().rstrip(".")

    @cached_property
    def blocked_self_domains(self) -> tuple[str, ...]:
        """Hostnames refused as shortener destinations (self-link prevention)."""
        return (self.system_default_domain,)

    # CORS — public API routes allow all origins (no credentials).
    # Private routes (auth, oauth, dashboard) require explicit origin allowlist.
    cors_origins: list[str] = ["*"]  # deprecated — kept for backward compat
    cors_private_origins: list[str] = []

    # Request body size limit (bytes); 1 MB default
    max_content_length: int = 1_048_576

    # GeoIP database paths (configurable for self-hosters)
    geoip_country_db: str = "misc/GeoLite2-Country.mmdb"
    geoip_city_db: str = "misc/GeoLite2-City.mmdb"

    # GitHub repository (owner/repo) — used for star count + outbound links
    github_repo: str = "spoo-me/spoo"

    # Analytics & tracking (leave empty to disable)
    clarity_id: str = ""

    # External service URLs
    contact_webhook: str = ""
    url_report_webhook: str = ""
    hcaptcha_secret: str = ""
    hcaptcha_sitekey: str = ""

    # Service limits (overridable by self-hosters via env vars)
    max_active_api_keys: int = 20
    max_date_range_days: int = 90
    http_client_timeout: float = 5.0

    # Validator constraints (overridable by self-hosters via env vars)
    blocked_url_regex_timeout: float = 0.2
    max_emoji_alias_length: int = 15
    # Newest Unicode emoji version accepted in custom emoji aliases.
    # 15.1 renders on iOS 17.4+ / Android 14+ / Windows 11 23H2+.
    emoji_accept_max_version: float = 15.1
    # Cap for auto-generated emoji aliases. 12.0 is the Windows 10 glyph
    # ceiling (Segoe UI Emoji's last update, May 2019) — generated codes
    # must render everywhere.
    emoji_generate_max_version: float = 12.0
    emoji_generated_alias_length: int = 3
    # Dark until geo destinations are visible to safety enforcement, which
    # keys on dest.host and cannot see them. See the tripwire in
    # tests/unit/test_geo_launch_guard.py.
    geo_rules_enabled: bool = False
    geo_rules_max_countries: int = 50
    url_password_min_length: int = 8
    account_password_min_length: int = 8
    account_password_max_length: int = 128

    # ── Field validators for safety-critical config ────────────────────

    @field_validator(
        "max_active_api_keys",
        "max_date_range_days",
        "max_emoji_alias_length",
        "emoji_generated_alias_length",
        "geo_rules_max_countries",
    )
    @classmethod
    def _must_be_positive_int(cls, v: int, info) -> int:
        if v < 1:
            raise ValueError(f"{info.field_name} must be >= 1, got {v}")
        return v

    @field_validator("emoji_accept_max_version", "emoji_generate_max_version")
    @classmethod
    def _emoji_version_cap_sane(cls, v: float, info) -> float:
        if v <= 0:
            raise ValueError(f"{info.field_name} must be > 0, got {v}")
        return v

    @model_validator(mode="after")
    def _emoji_generate_within_accept(self) -> AppSettings:
        # Generated aliases must themselves be accepted as custom aliases.
        if self.emoji_generate_max_version > self.emoji_accept_max_version:
            raise ValueError(
                "emoji_generate_max_version must be <= emoji_accept_max_version"
            )
        # generate_emoji_alias_v2 rejects lengths over 15, and generated
        # aliases must pass the acceptance grapheme cap — fail at boot,
        # not on every alias_type="emoji" request at runtime.
        if self.emoji_generated_alias_length > min(15, self.max_emoji_alias_length):
            raise ValueError(
                "emoji_generated_alias_length must be <= 15 and <= max_emoji_alias_length"
            )
        return self

    @field_validator("http_client_timeout", "blocked_url_regex_timeout")
    @classmethod
    def _must_be_positive_float(cls, v: float, info) -> float:
        if v <= 0:
            raise ValueError(f"{info.field_name} must be > 0, got {v}")
        return v

    @field_validator("url_password_min_length", "account_password_min_length")
    @classmethod
    def _password_min_length_sane(cls, v: int, info) -> int:
        if v < 1:
            raise ValueError(f"{info.field_name} must be >= 1, got {v}")
        return v

    @field_validator("account_password_max_length")
    @classmethod
    def _password_max_length_sane(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"account_password_max_length must be >= 1, got {v}")
        return v

    # Sub-configs (composed via model_validator below)
    db: DatabaseSettings | None = None
    redis: RedisSettings | None = None
    jwt: JWTSettings | None = None
    oauth: OAuthProviderSettings | None = None
    email: EmailSettings | None = None
    logging: LoggingSettings | None = None
    sentry: SentrySettings | None = None
    custom_domains: CustomDomainSettings | None = None
    click_events: ClickEventsSettings | None = None
    edge_cache: EdgeCacheSettings | None = None
    r2: R2StorageSettings | None = None
    meta_tags: MetaTagsSettings | None = None
    web_risk: WebRiskSettings | None = None
    webhooks: WebhookSettings | None = None
    safety: SafetySettings | None = None
    scheduler: SchedulerSettings | None = None
    llm: LlmSettings | None = None

    @model_validator(mode="after")
    def _populate_sub_configs_and_secret(self) -> AppSettings:
        # Cross-field validation
        if self.account_password_max_length < self.account_password_min_length:
            raise ValueError(
                f"account_password_max_length ({self.account_password_max_length}) "
                f"must be >= account_password_min_length ({self.account_password_min_length})"
            )

        # Accept FLASK_SECRET_KEY as a fallback for backward compatibility
        if not self.secret_key and self.flask_secret_key:
            self.secret_key = self.flask_secret_key

        # Populate sub-configs from the same env/dotenv source
        if self.db is None:
            self.db = DatabaseSettings()
        if self.redis is None:
            self.redis = RedisSettings()
        if self.jwt is None:
            self.jwt = JWTSettings()
        if self.oauth is None:
            self.oauth = OAuthProviderSettings()
        if self.email is None:
            self.email = EmailSettings()
        if self.logging is None:
            self.logging = LoggingSettings()
        if self.sentry is None:
            self.sentry = SentrySettings()
        if self.custom_domains is None:
            self.custom_domains = CustomDomainSettings()
        if self.click_events is None:
            self.click_events = ClickEventsSettings()
        if self.edge_cache is None:
            self.edge_cache = EdgeCacheSettings()
        if self.r2 is None:
            self.r2 = R2StorageSettings()
        if self.meta_tags is None:
            self.meta_tags = MetaTagsSettings()
        if self.web_risk is None:
            self.web_risk = WebRiskSettings()
        if self.webhooks is None:
            self.webhooks = WebhookSettings()
        if self.llm is None:
            self.llm = LlmSettings()
        if self.safety is None:
            self.safety = SafetySettings()
        if self.scheduler is None:
            self.scheduler = SchedulerSettings()
        if self.webhooks.enabled and not self.secret_key:
            # Signing secrets are encrypted with a key derived from
            # SECRET_KEY; an empty master would mean a predictable key.
            raise ValueError("SECRET_KEY must be set when WEBHOOKS_ENABLED=true")

        # The DCV mock makes domain verification succeed unconditionally —
        # in production that would let anyone claim any domain. Refuse to
        # boot rather than trust an env var to never be mis-set there.
        if self.env == "production" and self.custom_domains.mock_dcv:
            raise ValueError(
                "CUSTOM_DOMAINS_MOCK_DCV must not be enabled in production"
            )

        return self

    @property
    def is_production(self) -> bool:
        return self.env == "production"
