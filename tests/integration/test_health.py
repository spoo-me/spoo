"""Integration tests for GET /health."""

import os

from fastapi import FastAPI
from fastapi.testclient import TestClient

from middleware.error_handler import register_error_handlers
from routes.health_routes import router as health_router

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/")


def _build_test_app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(health_router)
    return app


class TestHealthEndpoint:
    def test_returns_ok(self):
        app = _build_test_app()
        with TestClient(app) as client:
            resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_no_db_dependency(self):
        """Health must not touch app.state.db or app.state.redis."""
        app = _build_test_app()
        with TestClient(app) as client:
            resp = client.get("/health")
        assert resp.status_code == 200
