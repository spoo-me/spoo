"""Tests for GET /api/v1/urls, GET /api/v1/urls/{url_id}, and
GET /api/v1/urls/{domain}/{alias}."""

from __future__ import annotations

from unittest.mock import AsyncMock

from bson import ObjectId
from fastapi.testclient import TestClient

from app.config import AppSettings
from app.dependencies import (
    get_current_user,
    get_settings,
    get_url_service,
    require_auth,
)
from app.errors import NotFoundError
from app.schemas.dto.responses.url import UrlListItem

from .conftest import _build_test_app, _make_api_key_doc, _make_url_doc, _make_user


class TestListUrls:
    def test_list_urls_requires_auth(self):
        application = _build_test_app(
            {get_current_user: lambda: None, get_url_service: lambda: AsyncMock()}
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/urls")

        assert resp.status_code == 401

    def test_list_urls_returns_paginated_response_with_camel_case(self):
        user = _make_user()
        list_result = {
            "items": [],
            "page": 1,
            "pageSize": 20,
            "total": 0,
            "hasNext": False,
            "sortBy": "created_at",
            "sortOrder": "descending",
        }
        mock_svc = AsyncMock()
        mock_svc.list_by_owner = AsyncMock(return_value=list_result)

        application = _build_test_app(
            {require_auth: lambda: user, get_url_service: lambda: mock_svc}
        )
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.get("/api/v1/urls")

        assert resp.status_code == 200
        body = resp.json()
        assert "hasNext" in body
        assert "pageSize" in body
        assert "sortBy" in body

    def test_list_urls_api_key_missing_scope_returns_403(self):
        key_doc = _make_api_key_doc(scopes=["shorten:create"])  # wrong scope
        user = _make_user(api_key_doc=key_doc)

        application = _build_test_app(
            {require_auth: lambda: user, get_url_service: lambda: AsyncMock()}
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/urls")

        assert resp.status_code == 403


class TestGetUrlById:
    """GET /api/v1/urls/{url_id} — single owned URL by ObjectId."""

    def test_get_url_returns_list_item_shape(self):
        user = _make_user()
        url_doc = _make_url_doc(owner_id=user.user_id)
        mock_svc = AsyncMock()
        mock_svc.get_owned = AsyncMock(return_value=url_doc)

        application = _build_test_app(
            {require_auth: lambda: user, get_url_service: lambda: mock_svc}
        )
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.get(f"/api/v1/urls/{url_doc.id}")

        assert resp.status_code == 200
        # Byte-identical to one element of GET /urls — same DTO, same fields.
        expected = UrlListItem.from_doc(url_doc).model_dump(mode="json")
        assert resp.json() == expected
        call_args = mock_svc.get_owned.call_args.args
        assert call_args == (url_doc.id, user.user_id)

    def test_get_url_malformed_id_returns_400(self):
        user = _make_user()
        mock_svc = AsyncMock()

        application = _build_test_app(
            {require_auth: lambda: user, get_url_service: lambda: mock_svc}
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/urls/not-an-objectid")

        assert resp.status_code == 400
        mock_svc.get_owned.assert_not_called()

    def test_get_url_foreign_id_returns_404(self):
        """Ownership is in the query — a foreign id answers exactly like a
        missing one (service raises NotFoundError for both)."""
        user = _make_user()
        mock_svc = AsyncMock()
        mock_svc.get_owned = AsyncMock(side_effect=NotFoundError("URL not found"))

        application = _build_test_app(
            {require_auth: lambda: user, get_url_service: lambda: mock_svc}
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.get(f"/api/v1/urls/{ObjectId()}")

        assert resp.status_code == 404

    def test_get_url_requires_auth(self):
        application = _build_test_app(
            {get_current_user: lambda: None, get_url_service: lambda: AsyncMock()}
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.get(f"/api/v1/urls/{ObjectId()}")

        assert resp.status_code == 401

    def test_get_url_api_key_missing_scope_returns_403(self):
        key_doc = _make_api_key_doc(scopes=["shorten:create"])  # wrong scope
        user = _make_user(api_key_doc=key_doc)

        application = _build_test_app(
            {require_auth: lambda: user, get_url_service: lambda: AsyncMock()}
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.get(f"/api/v1/urls/{ObjectId()}")

        assert resp.status_code == 403


class TestGetUrlByAddress:
    """GET /api/v1/urls/{domain}/{alias} — single owned URL by natural key."""

    @staticmethod
    def _overrides(user, mock_svc) -> dict:
        # Pin APP_URL so the system-domain resolution assertions are
        # deterministic regardless of any local .env.
        return {
            require_auth: lambda: user,
            get_url_service: lambda: mock_svc,
            get_settings: lambda: AppSettings(app_url="https://spoo.me"),
        }

    def test_default_domain_via_system_hostname(self):
        user = _make_user()
        url_doc = _make_url_doc(alias="testme", owner_id=user.user_id)
        mock_svc = AsyncMock()
        mock_svc.get_owned_by_alias = AsyncMock(return_value=url_doc)

        application = _build_test_app(self._overrides(user, mock_svc))
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.get("/api/v1/urls/spoo.me/testme")

        assert resp.status_code == 200
        expected = UrlListItem.from_doc(url_doc).model_dump(mode="json")
        assert resp.json() == expected
        call = mock_svc.get_owned_by_alias.call_args
        assert call.args == ("testme", user.user_id)
        assert call.kwargs == {"domain": "spoo.me"}

    def test_system_hostname_is_normalised_case_and_port(self):
        """Uppercase and :port forms of the system hostname resolve to the
        default-domain lookup, mirroring tenant host normalisation."""
        user = _make_user()
        url_doc = _make_url_doc(alias="testme", owner_id=user.user_id)
        mock_svc = AsyncMock()
        mock_svc.get_owned_by_alias = AsyncMock(return_value=url_doc)

        application = _build_test_app(self._overrides(user, mock_svc))
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.get("/api/v1/urls/SPOO.ME:443/testme")

        assert resp.status_code == 200
        assert mock_svc.get_owned_by_alias.call_args.kwargs == {"domain": "spoo.me"}

    def test_custom_domain_scopes_lookup(self):
        user = _make_user()
        url_doc = _make_url_doc(alias="promo", owner_id=user.user_id)
        url_doc.domain = "links.acme.com"
        mock_svc = AsyncMock()
        mock_svc.get_owned_by_alias = AsyncMock(return_value=url_doc)

        application = _build_test_app(self._overrides(user, mock_svc))
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.get("/api/v1/urls/links.acme.com/promo")

        assert resp.status_code == 200
        assert resp.json()["domain"] == "links.acme.com"
        call = mock_svc.get_owned_by_alias.call_args
        assert call.args == ("promo", user.user_id)
        assert call.kwargs == {"domain": "links.acme.com"}

    def test_emoji_alias_percent_encoded(self):
        user = _make_user()
        url_doc = _make_url_doc(alias="\U0001f40d\U0001f525", owner_id=user.user_id)
        mock_svc = AsyncMock()
        mock_svc.get_owned_by_alias = AsyncMock(return_value=url_doc)

        application = _build_test_app(self._overrides(user, mock_svc))
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.get("/api/v1/urls/spoo.me/%F0%9F%90%8D%F0%9F%94%A5")

        assert resp.status_code == 200
        assert resp.json()["alias"] == "\U0001f40d\U0001f525"
        call = mock_svc.get_owned_by_alias.call_args
        assert call.args == ("\U0001f40d\U0001f525", user.user_id)

    def test_emoji_alias_vs16_variant_canonicalised(self):
        """A pasted VS16 variant (``⭐️`` = star + U+FE0F) resolves to the
        canonical stored bare-star link — v2 stores canonical aliases only,
        so the endpoint must canonicalize before the exact-match lookup."""
        user = _make_user()
        url_doc = _make_url_doc(alias="⭐", owner_id=user.user_id)
        mock_svc = AsyncMock()
        mock_svc.get_owned_by_alias = AsyncMock(return_value=url_doc)

        application = _build_test_app(self._overrides(user, mock_svc))
        with TestClient(application, raise_server_exceptions=True) as client:
            # %E2%AD%90%EF%B8%8F == "⭐️" (U+2B50 U+FE0F)
            resp = client.get("/api/v1/urls/spoo.me/%E2%AD%90%EF%B8%8F")

        assert resp.status_code == 200
        assert resp.json()["alias"] == "⭐"
        call = mock_svc.get_owned_by_alias.call_args
        # Looked up under the canonical bare star, not the VS16 variant.
        assert call.args == ("⭐", user.user_id)

    def test_foreign_link_returns_404(self):
        user = _make_user()
        mock_svc = AsyncMock()
        mock_svc.get_owned_by_alias = AsyncMock(
            side_effect=NotFoundError("URL not found")
        )

        application = _build_test_app(self._overrides(user, mock_svc))
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/urls/spoo.me/someones-link")

        assert resp.status_code == 404

    def test_unknown_domain_returns_404(self):
        """A domain with no owned links answers like a missing link — the
        endpoint never confirms what exists outside the caller's account."""
        user = _make_user()
        mock_svc = AsyncMock()
        mock_svc.get_owned_by_alias = AsyncMock(
            side_effect=NotFoundError("URL not found")
        )

        application = _build_test_app(self._overrides(user, mock_svc))
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/urls/nobody.example.net/testme")

        assert resp.status_code == 404
        assert mock_svc.get_owned_by_alias.call_args.kwargs == {
            "domain": "nobody.example.net"
        }

    def test_status_blind_returns_disabled_link(self):
        """The owner sees non-active links with their status field — reads
        of your own inventory never 410."""
        user = _make_user()
        url_doc = _make_url_doc(alias="paused", owner_id=user.user_id)
        url_doc.status = "INACTIVE"
        mock_svc = AsyncMock()
        mock_svc.get_owned_by_alias = AsyncMock(return_value=url_doc)

        application = _build_test_app(self._overrides(user, mock_svc))
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.get("/api/v1/urls/spoo.me/paused")

        assert resp.status_code == 200
        assert resp.json()["status"] == "INACTIVE"

    def test_requires_auth(self):
        application = _build_test_app(
            {get_current_user: lambda: None, get_url_service: lambda: AsyncMock()}
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/urls/spoo.me/testme")

        assert resp.status_code == 401

    def test_api_key_missing_scope_returns_403(self):
        key_doc = _make_api_key_doc(scopes=["shorten:create"])  # wrong scope
        user = _make_user(api_key_doc=key_doc)

        application = _build_test_app(
            {require_auth: lambda: user, get_url_service: lambda: AsyncMock()}
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/urls/spoo.me/testme")

        assert resp.status_code == 403
