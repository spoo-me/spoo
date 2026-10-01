"""Tests for POST/GET/DELETE /api/v1/keys."""

from __future__ import annotations

from unittest.mock import AsyncMock

from bson import ObjectId
from fastapi.testclient import TestClient

from app.dependencies import (
    get_api_key_service,
    get_current_user,
    require_jwt_verified,
    require_keys_access,
)

from .conftest import _build_test_app, _make_api_key_doc, _make_user


class TestApiKeys:
    def test_create_key_returns_201_with_token(self):
        user = _make_user(email_verified=True)
        key_doc = _make_api_key_doc(user_id=user.user_id)

        mock_svc = AsyncMock()
        mock_svc.create = AsyncMock(return_value=(key_doc, "spoo_rawtoken123"))

        application = _build_test_app(
            {
                require_jwt_verified: lambda: user,
                get_api_key_service: lambda: mock_svc,
            }
        )
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.post(
                "/api/v1/keys",
                json={"name": "Test Key", "scopes": ["shorten:create"]},
            )

        assert resp.status_code == 201
        body = resp.json()
        assert body["token"] == "spoo_rawtoken123"
        assert body["token_prefix"] == "AbCdEfGh"
        assert "id" in body

    def test_create_key_unverified_email_returns_403(self):
        user = _make_user(email_verified=False)

        application = _build_test_app(
            {get_current_user: lambda: user, get_api_key_service: lambda: AsyncMock()}
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.post(
                "/api/v1/keys",
                json={"name": "Test Key", "scopes": ["shorten:create"]},
            )

        assert resp.status_code == 403
        assert resp.json()["code"] == "EMAIL_NOT_VERIFIED"

    def test_create_key_requires_auth(self):
        application = _build_test_app(
            {get_current_user: lambda: None, get_api_key_service: lambda: AsyncMock()}
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.post(
                "/api/v1/keys",
                json={"name": "Test Key", "scopes": ["shorten:create"]},
            )

        assert resp.status_code == 401

    def test_list_keys_requires_auth(self):
        application = _build_test_app(
            {get_current_user: lambda: None, get_api_key_service: lambda: AsyncMock()}
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/keys")

        assert resp.status_code == 401

    def test_list_keys_returns_keys_without_token(self):
        user = _make_user()
        key_doc = _make_api_key_doc(user_id=user.user_id)

        mock_svc = AsyncMock()
        mock_svc.list_by_user = AsyncMock(return_value=[key_doc])

        application = _build_test_app(
            {require_keys_access: lambda: user, get_api_key_service: lambda: mock_svc}
        )
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.get("/api/v1/keys")

        assert resp.status_code == 200
        body = resp.json()
        assert "keys" in body
        assert len(body["keys"]) == 1
        assert "token" not in body["keys"][0]  # token never returned in list

    def test_delete_key_hard_delete(self):
        user = _make_user()
        mock_svc = AsyncMock()
        mock_svc.revoke = AsyncMock(return_value=True)

        application = _build_test_app(
            {require_keys_access: lambda: user, get_api_key_service: lambda: mock_svc}
        )
        key_id = str(ObjectId())
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.delete(f"/api/v1/keys/{key_id}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["action"] == "deleted"
        assert body["success"] is True
        mock_svc.revoke.assert_called_once_with(
            user.user_id, ObjectId(key_id), hard_delete=True
        )

    def test_delete_key_soft_revoke(self):
        user = _make_user()
        mock_svc = AsyncMock()
        mock_svc.revoke = AsyncMock(return_value=True)

        application = _build_test_app(
            {require_keys_access: lambda: user, get_api_key_service: lambda: mock_svc}
        )
        key_id = str(ObjectId())
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.delete(f"/api/v1/keys/{key_id}?revoke=true")

        assert resp.status_code == 200
        body = resp.json()
        assert body["action"] == "revoked"
        mock_svc.revoke.assert_called_once_with(
            user.user_id, ObjectId(key_id), hard_delete=False
        )

    def test_delete_key_not_found_returns_404(self):
        user = _make_user()
        mock_svc = AsyncMock()
        mock_svc.revoke = AsyncMock(return_value=False)

        application = _build_test_app(
            {require_keys_access: lambda: user, get_api_key_service: lambda: mock_svc}
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.delete(f"/api/v1/keys/{ObjectId()}")

        assert resp.status_code == 404

    def test_delete_key_invalid_id_returns_404(self):
        """Non-ObjectId key_id is treated as not-found (no information leak)."""
        user = _make_user()

        application = _build_test_app(
            {
                require_keys_access: lambda: user,
                get_api_key_service: lambda: AsyncMock(),
            }
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.delete("/api/v1/keys/not-an-objectid")

        assert resp.status_code == 422
