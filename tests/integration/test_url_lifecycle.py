"""
Integration tests for URL lifecycle flows: create -> redirect -> stats -> update -> delete.

All DB / Redis / external-service calls are eliminated via
dependency_overrides and a mock lifespan — no real infrastructure needed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

from bson import ObjectId
from fastapi.testclient import TestClient

from app.dependencies import (
    CurrentUser,
    get_click_service,
    get_current_user,
    get_export_service,
    get_stats_service,
    get_url_service,
    require_auth,
)
from app.errors import GoneError, NotFoundError
from app.infrastructure.cache.url_cache import UrlCacheData
from app.routes.api_v1 import router as api_v1_router
from app.routes.redirect_routes import router as redirect_router
from app.schemas.models.url import UrlV2Doc
from tests.conftest import build_test_app

# ── Helpers ──────────────────────────────────────────────────────────────────

_OWNER_OID = ObjectId()
_URL_OID = ObjectId()
_NOW = datetime.now(timezone.utc)
_ALIAS = "abc123"
_LONG_URL = "https://www.example.com/very/long/path"


def _make_url_doc(
    alias: str = _ALIAS,
    long_url: str = _LONG_URL,
    owner_id: ObjectId = _OWNER_OID,
    status: str = "ACTIVE",
    password: str | None = None,
    max_clicks: int | None = None,
    total_clicks: int = 0,
) -> UrlV2Doc:
    return UrlV2Doc.from_mongo(
        {
            "_id": _URL_OID,
            "alias": alias,
            "owner_id": owner_id,
            "created_at": _NOW,
            "long_url": long_url,
            "password": password,
            "block_bots": False,
            "max_clicks": max_clicks,
            "expire_after": None,
            "status": status,
            "private_stats": True,
            "total_clicks": total_clicks,
            "last_click": None,
            "updated_at": _NOW,
            "domain": "spoo.me",
        }
    )


def _make_cache_data(
    alias: str = _ALIAS,
    long_url: str = _LONG_URL,
    status: str = "ACTIVE",
    password_hash: str | None = None,
    max_clicks: int | None = None,
) -> UrlCacheData:
    return UrlCacheData(
        id=str(_URL_OID),
        alias=alias,
        long_url=long_url,
        block_bots=False,
        password_hash=password_hash,
        expiration_time=None,
        max_clicks=max_clicks,
        url_status=status,
        schema_version="v2",
        owner_id=str(_OWNER_OID),
        total_clicks=0,
    )


def _mock_user() -> CurrentUser:
    return CurrentUser(user_id=_OWNER_OID, email_verified=True)


# ── Tests ────────────────────────────────────────────────────────────────────


def test_create_url_then_redirect():
    """Create a URL via POST /api/v1/shorten, then GET /{alias} redirects."""
    doc = _make_url_doc()
    cache = _make_cache_data()

    url_svc = AsyncMock()
    url_svc.create.return_value = (doc, None)
    url_svc.resolve.return_value = (cache, "v2")

    click_svc = AsyncMock()

    app = build_test_app(
        api_v1_router,
        redirect_router,
        overrides={
            get_url_service: lambda: url_svc,
            get_click_service: lambda: click_svc,
        },
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        # Step 1: Create URL
        resp = client.post("/api/v1/shorten", json={"url": _LONG_URL})
        assert resp.status_code == 201
        data = resp.json()
        assert data["alias"] == _ALIAS
        assert data["long_url"] == _LONG_URL

        # Step 2: Redirect
        resp = client.get(f"/{_ALIAS}", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == _LONG_URL


def test_create_url_with_custom_alias_then_redirect():
    """Create URL with a custom alias, then verify redirect works."""
    custom_alias = "my-custom"
    doc = _make_url_doc(alias=custom_alias)
    cache = _make_cache_data(alias=custom_alias)

    url_svc = AsyncMock()
    url_svc.create.return_value = (doc, None)
    url_svc.resolve.return_value = (cache, "v2")

    click_svc = AsyncMock()

    app = build_test_app(
        api_v1_router,
        redirect_router,
        overrides={
            get_url_service: lambda: url_svc,
            get_click_service: lambda: click_svc,
        },
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/api/v1/shorten",
            json={"url": _LONG_URL, "alias": custom_alias},
        )
        assert resp.status_code == 201
        assert resp.json()["alias"] == custom_alias

        resp = client.get(f"/{custom_alias}", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == _LONG_URL


def test_create_url_then_check_stats():
    """Create URL, then GET /api/v1/stats (authed) returns stats."""
    doc = _make_url_doc()

    url_svc = AsyncMock()
    url_svc.create.return_value = (doc, None)

    stats_result = {
        "scope": "all",
        "filters": {},
        "group_by": ["time"],
        "timezone": "UTC",
        "time_range": {"start_date": None, "end_date": None},
        "summary": {
            "total_clicks": 42,
            "unique_clicks": 30,
            "first_click": None,
            "last_click": None,
            "avg_redirection_time": 0.15,
        },
        "metrics": {},
    }
    stats_svc = AsyncMock()
    stats_svc.query.return_value = stats_result

    user = _mock_user()

    app = build_test_app(
        api_v1_router,
        redirect_router,
        overrides={
            get_url_service: lambda: url_svc,
            get_stats_service: lambda: stats_svc,
            get_current_user: lambda: user,
        },
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        # Step 1: Create
        resp = client.post("/api/v1/shorten", json={"url": _LONG_URL})
        assert resp.status_code == 201

        # Step 2: Stats
        resp = client.get(f"/api/v1/stats?short_code={_ALIAS}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["summary"]["total_clicks"] == 42
        assert data["scope"] == "all"


def test_create_url_then_update_long_url():
    """Create URL, then PATCH /api/v1/urls/{id} with new long_url."""
    doc = _make_url_doc()
    new_long = "https://example.com/updated"
    updated_doc = _make_url_doc(long_url=new_long)

    url_svc = AsyncMock()
    url_svc.create.return_value = (doc, None)
    url_svc.update.return_value = updated_doc

    user = _mock_user()

    app = build_test_app(
        api_v1_router,
        redirect_router,
        overrides={
            get_url_service: lambda: url_svc,
            require_auth: lambda: user,
            get_current_user: lambda: user,
        },
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        # Step 1: Create
        resp = client.post("/api/v1/shorten", json={"url": _LONG_URL})
        assert resp.status_code == 201

        # Step 2: Update
        resp = client.patch(
            f"/api/v1/urls/{_URL_OID}",
            json={"long_url": new_long},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["long_url"] == new_long


def test_create_url_then_deactivate():
    """Create URL, deactivate it, then GET /{alias} returns 410."""
    doc = _make_url_doc()
    deactivated_doc = _make_url_doc(status="INACTIVE")

    url_svc = AsyncMock()
    url_svc.create.return_value = (doc, None)
    url_svc.update.return_value = deactivated_doc
    # After deactivation, resolve raises GoneError
    url_svc.resolve.side_effect = GoneError("URL expired or inactive")

    click_svc = AsyncMock()
    user = _mock_user()

    app = build_test_app(
        api_v1_router,
        redirect_router,
        overrides={
            get_url_service: lambda: url_svc,
            get_click_service: lambda: click_svc,
            require_auth: lambda: user,
            get_current_user: lambda: user,
        },
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        # Step 1: Create
        resp = client.post("/api/v1/shorten", json={"url": _LONG_URL})
        assert resp.status_code == 201

        # Step 2: Deactivate
        resp = client.patch(
            f"/api/v1/urls/{_URL_OID}/status",
            json={"status": "INACTIVE"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "INACTIVE"

        # Step 3: Redirect attempt returns 410
        resp = client.get(f"/{_ALIAS}", follow_redirects=False)
        assert resp.status_code == 410


def test_create_url_then_delete():
    """Create URL, then DELETE /api/v1/urls/{id}."""
    doc = _make_url_doc()

    url_svc = AsyncMock()
    url_svc.create.return_value = (doc, None)
    url_svc.delete.return_value = None

    user = _mock_user()

    app = build_test_app(
        api_v1_router,
        redirect_router,
        overrides={
            get_url_service: lambda: url_svc,
            require_auth: lambda: user,
            get_current_user: lambda: user,
        },
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        # Step 1: Create
        resp = client.post("/api/v1/shorten", json={"url": _LONG_URL})
        assert resp.status_code == 201

        # Step 2: Delete
        resp = client.delete(f"/api/v1/urls/{_URL_OID}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["message"] == "URL deleted"
        assert data["id"] == str(_URL_OID)


def test_create_password_protected_then_redirect():
    """Create URL with password; GET without password shows password page, with password redirects."""
    pw_hash = "$argon2id$v=19$m=65536,t=3,p=4$..."
    doc = _make_url_doc(password=pw_hash)
    cache_no_pw = _make_cache_data(password_hash=pw_hash)

    url_svc = AsyncMock()
    url_svc.create.return_value = (doc, None)
    url_svc.resolve.return_value = (cache_no_pw, "v2")

    click_svc = AsyncMock()

    # Patch verify_password to simulate correct password check
    from unittest.mock import patch

    app = build_test_app(
        api_v1_router,
        redirect_router,
        overrides={
            get_url_service: lambda: url_svc,
            get_click_service: lambda: click_svc,
        },
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        # Step 1: Create
        resp = client.post("/api/v1/shorten", json={"url": _LONG_URL})
        assert resp.status_code == 201

        # Step 2: GET without password shows password page (401)
        resp = client.get(f"/{_ALIAS}", follow_redirects=False)
        assert resp.status_code == 401

        # Step 3: GET with correct password redirects
        with patch(
            "app.infrastructure.cache.url_cache.verify_password_hash", return_value=True
        ):
            resp = client.get(f"/{_ALIAS}?password=correct", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == _LONG_URL


def test_create_url_with_max_clicks_expires():
    """Create URL with max_clicks=1; first redirect works, second returns 410."""
    doc = _make_url_doc(max_clicks=1)
    cache = _make_cache_data(max_clicks=1)

    url_svc = AsyncMock()
    url_svc.create.return_value = (doc, None)
    # First resolve: ACTIVE; second resolve: GoneError
    url_svc.resolve.side_effect = [
        (cache, "v2"),
        GoneError("URL expired — max clicks reached"),
    ]

    click_svc = AsyncMock()

    app = build_test_app(
        api_v1_router,
        redirect_router,
        overrides={
            get_url_service: lambda: url_svc,
            get_click_service: lambda: click_svc,
        },
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        # Step 1: Create
        resp = client.post("/api/v1/shorten", json={"url": _LONG_URL})
        assert resp.status_code == 201

        # Step 2: First redirect works
        resp = client.get(f"/{_ALIAS}", follow_redirects=False)
        assert resp.status_code == 302

        # Step 3: Second redirect returns 410 (expired)
        resp = client.get(f"/{_ALIAS}", follow_redirects=False)
        assert resp.status_code == 410


def test_redirect_nonexistent_alias_returns_404():
    """GET /{alias} for non-existent alias returns 404."""
    url_svc = AsyncMock()
    url_svc.resolve.side_effect = NotFoundError("URL not found")
    click_svc = AsyncMock()

    app = build_test_app(
        api_v1_router,
        redirect_router,
        overrides={
            get_url_service: lambda: url_svc,
            get_click_service: lambda: click_svc,
        },
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/nonexistent", follow_redirects=False)
        assert resp.status_code == 404


def test_create_url_then_export_stats():
    """Create URL, then GET /api/v1/export returns file download."""
    doc = _make_url_doc()

    url_svc = AsyncMock()
    url_svc.create.return_value = (doc, None)

    from app.schemas.results import ExportResult

    export_svc = AsyncMock()
    export_svc.export.return_value = ExportResult(
        content=b'{"summary": {}}',
        mimetype="application/json",
        filename="stats.json",
    )

    user = _mock_user()

    app = build_test_app(
        api_v1_router,
        redirect_router,
        overrides={
            get_url_service: lambda: url_svc,
            get_export_service: lambda: export_svc,
            get_current_user: lambda: user,
        },
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        # Step 1: Create
        resp = client.post("/api/v1/shorten", json={"url": _LONG_URL})
        assert resp.status_code == 201

        # Step 2: Export
        resp = client.get(f"/api/v1/export?short_code={_ALIAS}&format=json")
        assert resp.status_code == 200
        assert "attachment" in resp.headers.get("content-disposition", "")
