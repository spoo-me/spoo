"""
FastAPI application factory.
create_app() is the single entry point for building the app.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import redis.asyncio as aioredis
import sentry_sdk
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pymongo.asynchronous.mongo_client import AsyncMongoClient
from starlette.middleware.sessions import SessionMiddleware

from config import AppSettings
from dependencies.wiring import wire_services
from infrastructure.bootstrap.system_default_domain import (
    ensure_system_default_domain,
)
from infrastructure.email.zeptomail import ZeptoMailProvider
from infrastructure.geoip import GeoIPService
from infrastructure.http_client import HttpClient
from infrastructure.logging import get_logger
from infrastructure.oauth_clients import OAUTH_STATE_TTL_SECONDS, init_oauth
from infrastructure.queue_redis import connect_queue_redis
from infrastructure.templates import configure_template_globals, templates
from middleware.error_handler import register_error_handlers
from middleware.logging import RequestLoggingMiddleware
from middleware.openapi import (
    API_CONTACT,
    API_DESCRIPTION,
    API_LICENSE,
    OPENAPI_TAGS,
    configure_openapi,
)
from middleware.rate_limiter import RateLimitHeadersMiddleware, limiter
from middleware.security import (
    MaxContentLengthMiddleware,
    SecurityHeadersMiddleware,
    StaticCacheHeadersMiddleware,
    configure_cors,
)
from middleware.tenant import TenantMiddleware
from repositories.feed_domain_repository import FeedDomainRepository
from repositories.indexes import ensure_indexes
from routes.api_v1 import router as api_v1_router
from routes.auth import router as auth_router
from routes.dashboard_routes import router as dashboard_router
from routes.health_routes import router as health_router
from routes.legacy.stats import router as legacy_stats_router
from routes.legacy.url_shortener import router as legacy_url_router
from routes.oauth_routes import router as oauth_router
from routes.redirect_routes import router as redirect_router
from routes.static_routes import router as static_router
from schemas.models.app import AppStatus
from services.safety.feeds import ensure_feed_seeds
from shared.app_registry import load_app_registry

log = get_logger(__name__)

_SCALAR_CDN = "https://cdn.jsdelivr.net/npm/@scalar/api-reference"
_DOCS_URL = "https://docs.spoo.me"


def create_app(settings: AppSettings | None = None) -> FastAPI:
    """Create and return a fully configured FastAPI application."""
    if settings is None:
        settings = AppSettings()

    # Initialise Sentry before anything else so it captures startup errors
    if settings.sentry.sentry_dsn:
        sentry_sdk.init(
            dsn=settings.sentry.sentry_dsn,
            send_default_pii=settings.sentry.sentry_send_pii,
            traces_sample_rate=settings.sentry.sentry_traces_sample_rate,
            profiles_sample_rate=settings.sentry.sentry_profile_sample_rate,
            release=settings.app_version if settings.app_version != "dev" else None,
            environment=settings.env,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # ── Startup ──────────────────────────────────────────────────────────
        mongo_client: AsyncMongoClient = AsyncMongoClient(
            settings.db.mongodb_uri,
            maxPoolSize=settings.db.max_pool_size,
            minPoolSize=settings.db.min_pool_size,
        )
        app.state.mongo_client = mongo_client
        app.state.db = mongo_client[settings.db.db_name]
        app.state.settings = settings

        # Redis is optional; self-hosters may not configure it
        redis_client = None
        if settings.redis.redis_uri:
            redis_client = aioredis.from_url(
                settings.redis.redis_uri,
                encoding="utf-8",
                decode_responses=True,
                socket_keepalive=True,
                health_check_interval=30,
            )
            try:
                await redis_client.ping()
                log.info(
                    "redis_connected", ttl_seconds=settings.redis.redis_ttl_seconds
                )
            except Exception as e:
                log.error("redis_connection_failed", error=str(e))
                redis_client = None
        else:
            log.warning(
                "redis_not_configured", detail="set REDIS_URI to enable caching"
            )
        app.state.redis = redis_client

        # Queue Redis (click event stream) — optional, SEPARATE instance from
        # the cache Redis (cache runs allkeys-lru which would evict stream
        # entries). None (unconfigured / unreachable / pre-8.2) degrades the
        # sink to inline in wiring.
        queue_redis = await connect_queue_redis(settings.click_events)
        app.state.queue_redis = queue_redis

        # OAuth clients — stored on app.state for route-layer access
        _, oauth_providers = init_oauth(settings.oauth)
        app.state.oauth_providers = oauth_providers

        # Shared HTTP client + email provider — singletons to preserve connection pooling
        http_client = HttpClient(timeout=settings.http_client_timeout)
        app.state.http_client = http_client
        app.state.email_provider = ZeptoMailProvider(
            settings.email, http_client, app_url=settings.app_url
        )

        # GeoIP — singleton so the .mmdb readers are opened once and reused
        app.state.geoip = GeoIPService(
            settings.geoip_country_db, settings.geoip_city_db
        )

        await ensure_system_default_domain(app.state.db, settings.system_default_domain)
        await ensure_indexes(
            app.state.db,
            webhook_log_ttl_seconds=settings.webhooks.delivery_log_ttl_seconds,
        )
        # First-boot feed seeds (empty feeds only): the live sets are owned
        # by the DB — operators and (later) the deep-analysis tier add
        # entries there, never in code.
        await ensure_feed_seeds(
            FeedDomainRepository(app.state.db["safety_feed_domains"])
        )

        # App registry for the consent/apps system
        app.state.app_registry = load_app_registry()
        templates.env.globals["show_apps"] = any(
            a.status == AppStatus.LIVE for a in app.state.app_registry.values()
        )

        # ── Build all repos + services (composition root) ────────────────
        wire_services(app, settings, redis_client)

        # Warn if session secret is missing when auth is enabled
        if settings.jwt and not settings.secret_key:
            log.warning(
                "secret_key_empty",
                detail="SECRET_KEY is empty — session cookies are unsigned. "
                "Set a strong SECRET_KEY when auth/OAuth is enabled.",
            )

        # Warn if CORS private origins not configured in production
        if settings.is_production and not settings.cors_private_origins:
            log.warning(
                "cors_private_origins_empty",
                detail="CORS_PRIVATE_ORIGINS is empty — auth/oauth/dashboard routes "
                "will reject all cross-origin requests. Set to your frontend domain(s).",
            )

        # Warn if JWT config is weak (auth is optional, so don't crash)
        if settings.jwt and bool(settings.jwt.jwt_private_key) != bool(
            settings.jwt.jwt_public_key
        ):
            log.warning(
                "jwt_rsa_half_configured",
                detail="Only one of JWT_PRIVATE_KEY / JWT_PUBLIC_KEY is set — "
                "both are required for RS256. Falling back to HS256.",
            )

        if settings.jwt and not settings.jwt.use_rs256:
            if not settings.jwt.jwt_secret:
                log.warning(
                    "jwt_config_insecure",
                    detail="RS256 keys not set and JWT_SECRET is empty — tokens can be forged. "
                    "Set JWT_PRIVATE_KEY + JWT_PUBLIC_KEY or a strong JWT_SECRET.",
                )
            elif len(settings.jwt.jwt_secret) < 32:
                log.warning(
                    "jwt_secret_weak",
                    detail="JWT_SECRET is shorter than 32 characters — consider using RS256 keys or a longer secret.",
                )

        # Embedded webhook delivery executor: the Mongo-only claim
        # loop runs here when no worker consumes — inline-rung deployments
        # keep retries/auto-disable without any extra process.
        webhook_executor_task: asyncio.Task | None = None
        if getattr(app.state, "webhook_executor_embedded", False):
            webhook_executor_task = asyncio.create_task(
                app.state.webhook_executor.run(), name="webhook-delivery-executor"
            )
            log.info("webhook_executor_embedded_started")

        # Embedded task scheduler: same Mongo-lease pattern — runs here
        # when the deploy has no worker to host it.
        task_scheduler_task: asyncio.Task | None = None
        if getattr(app.state, "task_scheduler_embedded", False):
            task_scheduler_task = asyncio.create_task(
                app.state.task_scheduler.run(), name="task-scheduler"
            )
            log.info("task_scheduler_embedded_started")

        yield

        # ── Shutdown ─────────────────────────────────────────────────────────
        if task_scheduler_task is not None:
            task_scheduler_task.cancel()
            with suppress(asyncio.CancelledError, asyncio.TimeoutError, TimeoutError):
                await asyncio.wait_for(task_scheduler_task, timeout=10)
        if webhook_executor_task is not None:
            webhook_executor_task.cancel()
            # Bounded: a delivery attempt mid-flight has a 15s HTTP timeout;
            # don't let a pathological one hold the whole app shutdown.
            with suppress(asyncio.CancelledError, asyncio.TimeoutError, TimeoutError):
                await asyncio.wait_for(webhook_executor_task, timeout=10)
        await mongo_client.close()
        if redis_client is not None:
            await redis_client.aclose()
        if queue_redis is not None:
            await queue_redis.aclose()
        await http_client.aclose()

    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        description=API_DESCRIPTION,
        contact=API_CONTACT,
        license_info=API_LICENSE,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
        openapi_tags=OPENAPI_TAGS,
    )

    configure_openapi(app, app_url=settings.app_url)

    # ── Template globals (tracking IDs available in every template) ─────
    configure_template_globals(
        clarity_id=settings.clarity_id,
        sentry_client_key=settings.sentry.client_key,
        hcaptcha_sitekey=settings.hcaptcha_sitekey,
    )

    # ── /docs — Scalar in dev, redirect in prod ──────────────────────────
    _is_prod = settings.is_production

    @app.get("/docs", include_in_schema=False)
    async def docs(request: Request):
        if _is_prod:
            return RedirectResponse(_DOCS_URL)
        return templates.TemplateResponse(
            request,
            "scalar_docs.html",
            {
                "app_name": settings.app_name,
                "scalar_cdn": _SCALAR_CDN,
            },
        )

    # ── Middleware (registered in reverse execution order) ────────────────
    # 1. Session — Authlib OAuth state is the only reader/writer; nothing
    #    else touches request.session. max_age doubles as the itsdangerous
    #    signature lifetime, so it tracks OAUTH_STATE_TTL_SECONDS and both
    #    halves of the flow expire together. same_site must stay lax: the
    #    provider returns the user with a top-level cross-site GET, and
    #    strict would drop the cookie on exactly that navigation. Secure is
    #    gated on is_production, the same gate HSTS uses, so plain-http
    #    local dev still works.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        max_age=OAUTH_STATE_TTL_SECONDS,
        same_site="lax",
        https_only=settings.is_production,
    )
    # 2. Security headers — must be outer so HSTS/CSP/nosniff apply to
    #    all responses including CORS preflights (204) and body-limit (413)
    app.add_middleware(SecurityHeadersMiddleware, hsts_enabled=settings.is_production)
    # 2a. Long-lived cache headers for /static/*
    app.add_middleware(StaticCacheHeadersMiddleware)
    # 3. CORS
    configure_cors(app, settings)
    # 4. Body size limit
    app.add_middleware(
        MaxContentLengthMiddleware, max_content_length=settings.max_content_length
    )
    # 5. Tenant resolution — populates request.state.tenant from Host
    app.add_middleware(TenantMiddleware)
    # 6. Request logging — logs all requests with request_id
    app.add_middleware(RequestLoggingMiddleware)
    # 7. Rate limit headers — placement is order-independent: the decorator
    #    writes view_rate_limit into shared scope state during endpoint
    #    execution, before any response starts flowing outward
    app.add_middleware(RateLimitHeadersMiddleware)

    # ── Error handlers + rate limiter ────────────────────────────────────
    app.state.limiter = limiter
    register_error_handlers(app)

    # ── Static files ─────────────────────────────────────────────────────
    _static_dir = os.path.join(os.path.dirname(__file__), "static")
    if os.path.isdir(_static_dir):
        app.mount("/static", StaticFiles(directory=_static_dir), name="static")

    # ── Routers ──────────────────────────────────────────────────────────
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(oauth_router)
    app.include_router(api_v1_router)
    app.include_router(dashboard_router)
    app.include_router(static_router)
    app.include_router(legacy_stats_router)
    # legacy_url_router and redirect_router last — both have catch-all /{short_code} variants
    app.include_router(legacy_url_router)
    app.include_router(redirect_router)

    return app
