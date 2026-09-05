"""X-Entitlements-Version rides every authenticated response."""

from __future__ import annotations

from unittest.mock import AsyncMock

from bson import ObjectId
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from middleware.entitlements import HEADER, EntitlementsVersionMiddleware

UID = ObjectId()


def _app(service=None):
    app = FastAPI()
    app.add_middleware(EntitlementsVersionMiddleware)
    if service is not None:
        app.state.entitlement_service = service

    @app.get("/anon")
    async def anon():
        return {"ok": True}

    @app.get("/resolved")
    async def resolved(request: Request):
        request.state.auth_ctx = {"user_id": str(UID)}
        request.state.entitlements_version = 12
        return {"ok": True}

    @app.get("/authed")
    async def authed(request: Request):
        request.state.auth_ctx = {"user_id": str(UID)}
        return {"ok": True}

    return TestClient(app)


def test_anonymous_response_has_no_header():
    resp = _app().get("/anon")
    assert HEADER not in resp.headers


def test_version_from_the_dependency_is_used_verbatim():
    service = AsyncMock()
    resp = _app(service).get("/resolved")
    assert resp.headers[HEADER] == "12"
    service.version_for.assert_not_awaited()


def test_authenticated_route_without_dependency_gets_one_lookup():
    service = AsyncMock()
    service.version_for = AsyncMock(return_value=4)
    resp = _app(service).get("/authed")
    assert resp.headers[HEADER] == "4"
    service.version_for.assert_awaited_once_with(UID)


def test_degraded_lookup_omits_the_header():
    service = AsyncMock()
    service.version_for = AsyncMock(return_value=None)
    resp = _app(service).get("/authed")
    assert HEADER not in resp.headers


def test_lookup_failure_never_breaks_the_response():
    service = AsyncMock()
    service.version_for = AsyncMock(side_effect=RuntimeError("redis"))
    resp = _app(service).get("/authed")
    assert resp.status_code == 200
    assert HEADER not in resp.headers
