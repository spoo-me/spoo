"""Tests for GET /api/v1/apps."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

from bson import ObjectId
from fastapi.testclient import TestClient

from app.dependencies import (
    get_app_grant_repo,
    get_app_registry,
    get_current_user,
    require_jwt,
)
from app.schemas.models.app import AppEntry
from app.schemas.models.app_grant import AppGrantDoc

from .conftest import _build_test_app, _make_api_key_doc, _make_user


def _make_grant(
    user_id: ObjectId,
    app_id: str = "spoo-cli",
    granted_at: datetime | None = None,
    last_used_at: datetime | None = None,
    scopes: list[str] | None = None,
) -> AppGrantDoc:
    return AppGrantDoc.from_mongo(
        {
            "_id": ObjectId(),
            "user_id": user_id,
            "app_id": app_id,
            # Naive on purpose: PyMongo returns naive datetimes, and the DTO
            # serializer must stamp UTC on the wire.
            "granted_at": granted_at or datetime(2026, 6, 10, 12, 0, 0),
            "last_used_at": last_used_at,
            "revoked_at": None,
            "scopes": scopes,
        }
    )


def _registry() -> dict[str, AppEntry]:
    return {
        "spoo-cli": AppEntry(
            name="Spoo CLI",
            icon="spoo-cli.svg",
            description="Shorten links from your terminal",
            status="live",
            scopes=["shorten:create", "stats:read"],
        )
    }


class TestListAppGrants:
    def test_requires_auth(self):
        application = _build_test_app(
            {get_current_user: lambda: None, get_app_grant_repo: lambda: AsyncMock()}
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/apps")

        assert resp.status_code == 401

    def test_api_key_auth_rejected(self):
        user = _make_user(api_key_doc=_make_api_key_doc())

        application = _build_test_app(
            {get_current_user: lambda: user, get_app_grant_repo: lambda: AsyncMock()}
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/apps")

        assert resp.status_code == 403

    def test_no_grants_returns_empty_items(self):
        user = _make_user()
        mock_repo = AsyncMock()
        mock_repo.find_active_for_user = AsyncMock(return_value=[])

        application = _build_test_app(
            {require_jwt: lambda: user, get_app_grant_repo: lambda: mock_repo}
        )
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.get("/api/v1/apps")

        assert resp.status_code == 200
        assert resp.json() == {"items": []}
        mock_repo.find_active_for_user.assert_called_once_with(user.user_id)

    def test_grant_scopes_snapshot_wins_over_registry(self):
        user = _make_user()
        grant = _make_grant(user.user_id, scopes=["shorten:create"])
        mock_repo = AsyncMock()
        mock_repo.find_active_for_user = AsyncMock(return_value=[grant])

        application = _build_test_app(
            {
                require_jwt: lambda: user,
                get_app_grant_repo: lambda: mock_repo,
                get_app_registry: lambda: _registry(),
            }
        )
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.get("/api/v1/apps")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 1
        item = body["items"][0]
        assert item == {
            "id": str(grant.id),
            "app": "spoo-cli",
            "app_name": "Spoo CLI",
            "icon": "spoo-cli.svg",
            "scopes": ["shorten:create"],
            "permissions": ["Create short links"],
            "granted_at": "2026-06-10T12:00:00+00:00",
            "last_used_at": None,
        }

    def test_legacy_grant_inherits_registry_scopes(self):
        """Grant minted before scoped consent: registry scopes apply."""
        user = _make_user()
        grant = _make_grant(user.user_id, scopes=None)
        mock_repo = AsyncMock()
        mock_repo.find_active_for_user = AsyncMock(return_value=[grant])

        application = _build_test_app(
            {
                require_jwt: lambda: user,
                get_app_grant_repo: lambda: mock_repo,
                get_app_registry: lambda: _registry(),
            }
        )
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.get("/api/v1/apps")

        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["scopes"] == ["shorten:create", "stats:read"]
        assert item["permissions"] == ["Create short links", "Read analytics data"]

    def test_grant_without_registry_entry_falls_back(self):
        user = _make_user()
        grant = _make_grant(user.user_id, app_id="spoo-retired")
        mock_repo = AsyncMock()
        mock_repo.find_active_for_user = AsyncMock(return_value=[grant])

        application = _build_test_app(
            {
                require_jwt: lambda: user,
                get_app_grant_repo: lambda: mock_repo,
                get_app_registry: lambda: _registry(),
            }
        )
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.get("/api/v1/apps")

        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["app"] == "spoo-retired"
        assert item["app_name"] == "spoo-retired"
        assert item["icon"] is None
        # Never understate access: the grant is still full-account even
        # when its catalogue entry is gone. Empty scopes = legacy
        # unrestricted on the wire.
        assert item["scopes"] == []
        assert item["permissions"] == ["Full access to your spoo.me account"]

    def test_grants_sorted_newest_granted_first(self):
        user = _make_user()
        older = _make_grant(
            user.user_id,
            app_id="spoo-retired",
            granted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        newer = _make_grant(
            user.user_id,
            app_id="spoo-cli",
            granted_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            last_used_at=datetime(2026, 7, 12, 9, 30, 41, tzinfo=timezone.utc),
        )
        mock_repo = AsyncMock()
        mock_repo.find_active_for_user = AsyncMock(return_value=[older, newer])

        application = _build_test_app(
            {
                require_jwt: lambda: user,
                get_app_grant_repo: lambda: mock_repo,
                get_app_registry: lambda: _registry(),
            }
        )
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.get("/api/v1/apps")

        assert resp.status_code == 200
        items = resp.json()["items"]
        assert [i["app"] for i in items] == ["spoo-cli", "spoo-retired"]
        assert items[0]["last_used_at"] == "2026-07-12T09:30:41+00:00"
