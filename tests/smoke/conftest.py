"""Shared fixtures for smoke tests."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/")

from app.config import AppSettings
from app.middleware.error_handler import register_error_handlers
from app.middleware.logging import RequestLoggingMiddleware
from app.middleware.rate_limiter import limiter
from app.middleware.security import (
    MaxContentLengthMiddleware,
    SecurityHeadersMiddleware,
    configure_cors,
)
from app.routes.api_v1 import router as api_v1_router
from app.routes.auth import router as auth_router
from app.routes.dashboard_routes import router as dashboard_router
from app.routes.health_routes import router as health_router
from app.routes.legacy.stats import router as legacy_stats_router
from app.routes.legacy.url_shortener import router as legacy_url_router
from app.routes.oauth_routes import router as oauth_router
from app.routes.redirect_routes import router as redirect_router
from app.routes.static_routes import router as static_router


def _build_smoke_app() -> FastAPI:
    settings = AppSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.db = MagicMock()
        app.state.redis = None
        app.state.email_provider = MagicMock()
        app.state.http_client = MagicMock()
        app.state.oauth_providers = {}
        app.state.geoip = MagicMock()
        # Mock services for singleton dependency lookups
        app.state.url_service = AsyncMock()
        app.state.stats_service = AsyncMock()
        app.state.export_service = AsyncMock()
        app.state.api_key_service = AsyncMock()
        app.state.credential_service = AsyncMock()
        app.state.verification_service = AsyncMock()
        app.state.password_service = AsyncMock()
        app.state.device_auth_service = AsyncMock()
        app.state.user_repo = AsyncMock()
        app.state.token_factory = AsyncMock()
        app.state.oauth_service = AsyncMock()
        app.state.profile_picture_service = AsyncMock()
        app.state.contact_service = AsyncMock()
        app.state.click_service = AsyncMock()
        app.state.feature_flag_service = AsyncMock()
        app.state.custom_domain_service = AsyncMock()
        app.state.tenant_resolver = AsyncMock()
        yield

    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        docs_url=None,
        lifespan=lifespan,
    )

    # Middleware (same order as factory.py)
    app.add_middleware(
        SessionMiddleware, secret_key=settings.secret_key or "test-secret"
    )
    configure_cors(app, settings)
    app.add_middleware(SecurityHeadersMiddleware, hsts_enabled=False)
    app.add_middleware(
        MaxContentLengthMiddleware, max_content_length=settings.max_content_length
    )
    app.add_middleware(RequestLoggingMiddleware)

    app.state.limiter = limiter
    register_error_handlers(app)

    # Static files
    _static_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "app", "static"
    )
    if os.path.isdir(_static_dir):
        app.mount("/static", StaticFiles(directory=_static_dir), name="static")

    # All routers (same order as factory.py)
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(oauth_router)
    app.include_router(api_v1_router)
    app.include_router(dashboard_router)
    app.include_router(static_router)
    app.include_router(legacy_stats_router)
    app.include_router(legacy_url_router)
    app.include_router(redirect_router)

    return app


@pytest.fixture
def smoke_app() -> FastAPI:
    return _build_smoke_app()


@pytest.fixture
def smoke_client(smoke_app: FastAPI) -> TestClient:
    with TestClient(smoke_app, raise_server_exceptions=False) as c:
        yield c
