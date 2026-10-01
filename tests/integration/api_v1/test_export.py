"""Tests for GET /api/v1/export and GET /api/v1/export/links/{url_id}."""

from __future__ import annotations

from unittest.mock import AsyncMock

from bson import ObjectId
from fastapi.testclient import TestClient

from app.dependencies import get_current_user, get_export_service, get_url_service
from app.errors import NotFoundError
from app.schemas.results import ExportResult

from .conftest import _build_test_app, _make_api_key_doc, _make_url_doc, _make_user


class TestExport:
    def test_export_json_returns_correct_content_type(self):
        user = _make_user()
        mock_svc = AsyncMock()
        mock_svc.export = AsyncMock(
            return_value=ExportResult(
                content=b'{"data": []}',
                mimetype="application/json",
                filename="stats.json",
            )
        )

        application = _build_test_app(
            {get_current_user: lambda: user, get_export_service: lambda: mock_svc}
        )
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.get("/api/v1/export?format=json")

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        assert "content-disposition" in resp.headers

    def test_export_unauthenticated_returns_401(self):
        application = _build_test_app(
            {get_current_user: lambda: None, get_export_service: lambda: AsyncMock()}
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/export?format=json")

        assert resp.status_code == 401

    def test_export_stray_scope_param_silently_ignored(self):
        user = _make_user()
        mock_svc = AsyncMock()
        mock_svc.export = AsyncMock(
            return_value=ExportResult(
                content=b'{"data": []}',
                mimetype="application/json",
                filename="stats.json",
            )
        )

        application = _build_test_app(
            {get_current_user: lambda: user, get_export_service: lambda: mock_svc}
        )
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.get("/api/v1/export?format=json&scope=anon")

        assert resp.status_code == 200
        assert not hasattr(mock_svc.export.call_args[0][0], "scope")

    def test_export_missing_format_returns_422(self):
        application = _build_test_app(
            {
                get_current_user: lambda: _make_user(),
                get_export_service: lambda: AsyncMock(),
            }
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/export")

        # Missing required `format` field → Pydantic validation → 422
        assert resp.status_code == 422

    def test_export_invalid_format_returns_422(self):
        application = _build_test_app(
            {
                get_current_user: lambda: _make_user(),
                get_export_service: lambda: AsyncMock(),
            }
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/export?format=pdf")

        assert resp.status_code == 422

    def test_export_api_key_missing_scope_returns_403(self):
        key_doc = _make_api_key_doc(scopes=["shorten:create"])
        user = _make_user(api_key_doc=key_doc)

        application = _build_test_app(
            {get_current_user: lambda: user, get_export_service: lambda: AsyncMock()}
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/export?format=json")

        assert resp.status_code == 403


class TestLinkExport:
    _URL_ID = str(ObjectId("e" * 24))

    def _app(self, *, user=None, export_svc=None, url_svc=None):
        return _build_test_app(
            {
                get_current_user: lambda: user,
                get_export_service: lambda: export_svc or AsyncMock(),
                get_url_service: lambda: url_svc or AsyncMock(),
            }
        )

    def test_link_export_happy_path(self):
        user = _make_user()
        url_doc = _make_url_doc(alias="mylink", owner_id=user.user_id)
        url_svc = AsyncMock()
        url_svc.get_owned = AsyncMock(return_value=url_doc)
        export_svc = AsyncMock()
        export_svc.export_link = AsyncMock(
            return_value=ExportResult(
                content=b'{"data": []}',
                mimetype="application/json",
                filename="spoo-me-export-mylink.json",
            )
        )

        application = self._app(user=user, export_svc=export_svc, url_svc=url_svc)
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.get(f"/api/v1/export/links/{self._URL_ID}?format=json")

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        assert "spoo-me-export-mylink.json" in resp.headers["content-disposition"]
        url_svc.get_owned.assert_awaited_once_with(ObjectId(self._URL_ID), user.user_id)
        export_svc.export_link.assert_awaited_once()

    def test_link_export_foreign_id_returns_404(self):
        user = _make_user()
        url_svc = AsyncMock()
        url_svc.get_owned = AsyncMock(side_effect=NotFoundError("URL not found"))
        export_svc = AsyncMock()

        application = self._app(user=user, export_svc=export_svc, url_svc=url_svc)
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.get(f"/api/v1/export/links/{self._URL_ID}?format=json")

        assert resp.status_code == 404
        export_svc.export_link.assert_not_awaited()

    def test_link_export_invalid_object_id_returns_400(self):
        application = self._app(user=_make_user())
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/export/links/not-an-objectid?format=json")

        assert resp.status_code == 400

    def test_link_export_unauthenticated_returns_401(self):
        application = self._app(user=None)
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.get(f"/api/v1/export/links/{self._URL_ID}?format=json")

        assert resp.status_code == 401

    def test_link_export_missing_format_returns_422(self):
        application = self._app(user=_make_user())
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.get(f"/api/v1/export/links/{self._URL_ID}")

        assert resp.status_code == 422
