"""
Root test conftest — shared test app builder.

Provides ``build_test_app()`` for integration tests that need a minimal
FastAPI app with mock infrastructure and dependency overrides.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi import APIRouter, FastAPI
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/")
# Tests sign/verify JWTs with the HS256 symmetric secret. CI (and a typical local
# .env) does not set JWT_SECRET, leaving it "" — and pyjwt >= 2.13 rejects empty
# HMAC keys ("HMAC key must not be empty."). Provide a non-prod test secret here,
# before any test module constructs AppSettings(), so signing/verification match.
# Set when missing *or* empty (setdefault would keep an explicit JWT_SECRET="").
if not os.environ.get("JWT_SECRET"):
    os.environ["JWT_SECRET"] = "test-jwt-secret-not-for-production"

from config import AppSettings
from middleware.error_handler import register_error_handlers
from middleware.rate_limiter import limiter
from services.entitlements import for_plan
from services.features.catalog import Plan

_STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static"
)


def build_test_app(
    *routers: APIRouter,
    overrides: dict | None = None,
    extra_state: dict[str, Any] | None = None,
    mount_static: bool = True,
) -> FastAPI:
    """Build a minimal FastAPI app with mock infrastructure for testing.

    Args:
        *routers:     Routers to include (e.g. ``auth_router``, ``api_v1_router``).
        overrides:    Dependency overrides dict (e.g. ``{get_credential_service: lambda: mock}``).
        extra_state:  Additional attributes to set on ``app.state`` beyond defaults.
        mount_static: Whether to mount the /static directory (default True).

    The default lifespan sets:
        - ``app.state.settings`` — real ``AppSettings()``
        - ``app.state.db`` — ``MagicMock()``
        - ``app.state.redis`` — ``None``
        - ``app.state.email_provider`` — ``MagicMock()``
        - ``app.state.http_client`` — ``MagicMock()``
        - ``app.state.oauth_providers`` — ``{}``
        - ``app.state.app_registry`` — ``{}``
        - ``app.state.credential_service`` — ``AsyncMock()``
        - ``app.state.verification_service`` — ``AsyncMock()``
        - ``app.state.password_service`` — ``AsyncMock()``
        - ``app.state.device_auth_service`` — ``AsyncMock()``
        - ``app.state.user_repo`` — ``AsyncMock()``
        - ``app.state.token_factory`` — ``AsyncMock()``

    Use ``extra_state`` to override or extend any of these defaults.
    """
    settings = AppSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.db = MagicMock()
        app.state.redis = None
        app.state.email_provider = MagicMock()
        app.state.http_client = MagicMock()
        app.state.oauth_providers = {}
        app.state.app_registry = {}
        # Service mocks (so dependency getters that read app.state don't fail)
        app.state.credential_service = AsyncMock()
        app.state.verification_service = AsyncMock()
        app.state.password_service = AsyncMock()
        app.state.device_auth_service = AsyncMock()
        app.state.user_repo = AsyncMock()
        app.state.token_factory = AsyncMock()
        app.state.url_service = AsyncMock()
        app.state.tag_service = AsyncMock()
        app.state.tag_service.refs_by_id = AsyncMock(return_value={})
        app.state.stats_service = AsyncMock()
        app.state.export_service = AsyncMock()
        app.state.api_key_service = AsyncMock()
        app.state.page_layout_service = AsyncMock()
        app.state.oauth_service = AsyncMock()
        app.state.profile_picture_service = AsyncMock()
        app.state.contact_service = AsyncMock()
        app.state.click_service = AsyncMock()
        app.state.click_sink = AsyncMock()
        app.state.app_grant_repo = AsyncMock()
        app.state.custom_domain_service = AsyncMock()
        app.state.feature_flag_service = AsyncMock()
        app.state.entitlement_service = AsyncMock()
        app.state.entitlement_service.resolve_for = AsyncMock(
            return_value=for_plan(Plan.FREE)
        )
        app.state.entitlement_service.usage_for = AsyncMock(return_value={})
        app.state.entitlement_service.plan_hint_for = AsyncMock(return_value="free")
        app.state.entitlement_service.over_limit_for = AsyncMock(return_value={})
        app.state.tenant_resolver = AsyncMock()
        # A REAL permissive L0 gate (no providers): valid URLs pass,
        # invalid/self-links reject — matches the pre-gate route behavior.
        # Tests exercising blocks override get_url_policy instead.
        from services.safety.policy import UrlPolicyService

        app.state.url_policy = UrlPolicyService(
            [], blocked_self_domains=[settings.system_default_domain]
        )
        if extra_state:
            for key, value in extra_state.items():
                setattr(app.state, key, value)
        yield

    application = FastAPI(lifespan=lifespan)
    application.state.limiter = limiter
    # Set outside the lifespan — some tests build TestClient without a `with`
    # block (no lifespan run) and the redirect route always resolves GeoIP.
    application.state.geoip = AsyncMock()
    # Same reason for these three: the Entitled dependency and the auth
    # dependency read them on every request, lifespan or not.
    application.state.db = MagicMock()
    application.state.settings = settings
    application.state.entitlement_service = AsyncMock()
    application.state.entitlement_service.resolve_for = AsyncMock(
        return_value=for_plan(Plan.FREE)
    )
    application.state.entitlement_service.usage_for = AsyncMock(return_value={})
    application.state.entitlement_service.over_limit_for = AsyncMock(return_value={})
    application.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    register_error_handlers(application)

    if mount_static and os.path.isdir(_STATIC_DIR):
        application.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    for router in routers:
        application.include_router(router)

    if overrides:
        application.dependency_overrides.update(overrides)

    return application
