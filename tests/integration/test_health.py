"""Integration tests for GET /health."""

import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware.error_handler import register_error_handlers
from app.routes.health_routes import router as health_router

# Ensure a MONGODB_URI is present so AppSettings can be instantiated
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/")


def _build_test_app(
    mongo_ok: bool = True,
    redis_ok: bool = True,
    redis_configured: bool = True,
) -> FastAPI:
    """
    Build a minimal FastAPI app with mocked DB/Redis injected via lifespan.
    No real network connections are made.
    """
    mock_db = MagicMock()
    if mongo_ok:
        mock_db.client.admin.command = AsyncMock(return_value={"ok": 1})
    else:
        mock_db.client.admin.command = AsyncMock(
            side_effect=Exception("connection refused")
        )

    if not redis_configured:
        mock_redis = None
    else:
        mock_redis = AsyncMock()
        if redis_ok:
            mock_redis.ping = AsyncMock(return_value=True)
        else:
            mock_redis.ping = AsyncMock(side_effect=Exception("redis down"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.db = mock_db
        app.state.redis = mock_redis
        yield

    app = FastAPI(lifespan=lifespan)
    register_error_handlers(app)
    app.include_router(health_router)
    return app


class TestHealthEndpoint:
    def test_healthy_when_both_ok(self):
        app = _build_test_app(mongo_ok=True, redis_ok=True)
        with TestClient(app) as client:
            resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "healthy"
        assert body["checks"]["mongodb"] == "ok"
        assert body["checks"]["redis"] == "ok"

    def test_unhealthy_when_mongo_fails(self):
        app = _build_test_app(mongo_ok=False, redis_ok=True)
        with TestClient(app) as client:
            resp = client.get("/health")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "unhealthy"
        assert body["checks"]["mongodb"] == "error"

    def test_degraded_when_redis_fails(self):
        app = _build_test_app(mongo_ok=True, redis_ok=False)
        with TestClient(app) as client:
            resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["checks"]["redis"] == "error"

    def test_degraded_when_redis_not_configured(self):
        app = _build_test_app(mongo_ok=True, redis_configured=False)
        with TestClient(app) as client:
            resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["checks"]["redis"] == "not_configured"

    def test_response_shape(self):
        app = _build_test_app()
        with TestClient(app) as client:
            resp = client.get("/health")
        body = resp.json()
        assert "status" in body
        assert "checks" in body
        assert "mongodb" in body["checks"]
        assert "redis" in body["checks"]
