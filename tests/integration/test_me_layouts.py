"""
Integration tests for per-user page layouts.

GET    /api/v1/me/layouts/{page} -> fetch saved layout (null = default)
PUT    /api/v1/me/layouts/{page} -> save layout doc verbatim
DELETE /api/v1/me/layouts/{page} -> reset to default (idempotent 204)

All DB / Redis / external-service calls are eliminated via
dependency_overrides and a mock lifespan — no real infrastructure needed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from bson import ObjectId
from fastapi.testclient import TestClient

from app.dependencies import CurrentUser, get_page_layout_service, require_jwt
from app.errors import AuthenticationError
from app.routes.api_v1 import router as api_v1_router
from tests.conftest import build_test_app

# ── Helpers ──────────────────────────────────────────────────────────────────

_USER_ID = ObjectId()

_DOC = {"version": 1, "widgets": [{"id": "w_time", "kind": "timeseries"}]}


def _make_user() -> CurrentUser:
    return CurrentUser(user_id=_USER_ID, email_verified=True, api_key_doc=None)


def _build(mock_svc: AsyncMock):
    app = build_test_app(
        api_v1_router,
        overrides={
            require_jwt: _make_user,
            get_page_layout_service: lambda: mock_svc,
        },
    )
    return TestClient(app, raise_server_exceptions=False)


def _raise_unauth() -> CurrentUser:
    raise AuthenticationError("Authentication required")


# ── GET ──────────────────────────────────────────────────────────────────────


def test_get_layout_returns_null_when_unset():
    svc = AsyncMock()
    svc.get_layout = AsyncMock(return_value=None)
    client = _build(svc)

    resp = client.get("/api/v1/me/layouts/analytics")

    assert resp.status_code == 200
    assert resp.json() == {"layout": None}
    args = svc.get_layout.await_args.args
    assert args == (_USER_ID, "analytics")


def test_get_layout_returns_saved_doc():
    svc = AsyncMock()
    svc.get_layout = AsyncMock(return_value=_DOC)
    client = _build(svc)

    resp = client.get("/api/v1/me/layouts/analytics")

    assert resp.status_code == 200
    assert resp.json() == {"layout": _DOC}


def test_get_layout_rejects_bad_page_pattern():
    client = _build(AsyncMock())

    resp = client.get("/api/v1/me/layouts/Bad%20Page!")

    assert resp.status_code == 422


def test_get_layout_rejects_unknown_page_slug():
    # Well-formed but not in the allowlist: the page namespace is a closed
    # set so one user can't accumulate unbounded junk layout docs.
    client = _build(AsyncMock())

    resp = client.get("/api/v1/me/layouts/junk-page")

    assert resp.status_code == 422


def test_overview_page_is_allowlisted():
    svc = AsyncMock()
    svc.get_layout = AsyncMock(return_value=None)
    client = _build(svc)

    resp = client.get("/api/v1/me/layouts/overview")

    assert resp.status_code == 200


# ── PUT ──────────────────────────────────────────────────────────────────────


def test_put_layout_roundtrips():
    svc = AsyncMock()
    svc.put_layout = AsyncMock(return_value=_DOC)
    client = _build(svc)

    resp = client.put("/api/v1/me/layouts/analytics", json={"layout": _DOC})

    assert resp.status_code == 200
    assert resp.json() == {"layout": _DOC}
    args = svc.put_layout.await_args.args
    assert args == (_USER_ID, "analytics", _DOC)


def test_put_layout_rejects_non_object():
    client = _build(AsyncMock())

    for bad in ([1, 2, 3], "nope", 42, None):
        resp = client.put("/api/v1/me/layouts/analytics", json={"layout": bad})
        assert resp.status_code == 422, f"expected 422 for {bad!r}"


def test_put_layout_rejects_oversized_doc():
    client = _build(AsyncMock())
    huge = {"version": 1, "blob": "x" * (33 * 1024)}

    resp = client.put("/api/v1/me/layouts/analytics", json={"layout": huge})

    assert resp.status_code == 422


# ── DELETE ───────────────────────────────────────────────────────────────────


def test_delete_layout_returns_204():
    svc = AsyncMock()
    svc.delete_layout = AsyncMock(return_value=True)
    client = _build(svc)

    resp = client.delete("/api/v1/me/layouts/analytics")

    assert resp.status_code == 204
    assert resp.content == b""


def test_delete_layout_idempotent_when_nothing_saved():
    svc = AsyncMock()
    svc.delete_layout = AsyncMock(return_value=False)
    client = _build(svc)

    resp = client.delete("/api/v1/me/layouts/analytics")

    assert resp.status_code == 204


# ── Auth ─────────────────────────────────────────────────────────────────────


def test_layouts_require_auth():
    app = build_test_app(
        api_v1_router,
        overrides={
            require_jwt: _raise_unauth,
            get_page_layout_service: lambda: AsyncMock(),
        },
    )
    client = TestClient(app, raise_server_exceptions=False)

    assert client.get("/api/v1/me/layouts/analytics").status_code == 401
    assert (
        client.put("/api/v1/me/layouts/analytics", json={"layout": {}}).status_code
        == 401
    )
    assert client.delete("/api/v1/me/layouts/analytics").status_code == 401
