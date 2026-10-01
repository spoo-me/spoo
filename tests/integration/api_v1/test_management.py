"""Tests for PATCH/DELETE /api/v1/urls/{url_id}."""

from __future__ import annotations

import typing
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

from app.dependencies import (
    get_custom_domain_service,
    get_url_service,
    require_auth,
)
from app.errors import ForbiddenError, NotFoundError
from app.schemas.models.url import AbVariant

from .conftest import _build_test_app, _make_api_key_doc, _make_url_doc, _make_user


class TestManagement:
    def test_update_url_returns_200(self):
        user = _make_user()
        url_doc = _make_url_doc(owner_id=user.user_id)
        url_doc.updated_at = datetime(2024, 6, 1, tzinfo=timezone.utc)

        mock_svc = AsyncMock()
        mock_svc.update = AsyncMock(return_value=url_doc)

        application = _build_test_app(
            {require_auth: lambda: user, get_url_service: lambda: mock_svc}
        )
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.patch(
                f"/api/v1/urls/{ObjectId()}", json={"status": "INACTIVE"}
            )

        assert resp.status_code == 200
        body = resp.json()
        assert "id" in body
        assert "alias" in body
        assert "password_set" in body
        assert "updated_at" in body

    def test_update_url_not_found_returns_404(self):
        user = _make_user()
        mock_svc = AsyncMock()
        mock_svc.update = AsyncMock(side_effect=NotFoundError("URL not found"))

        application = _build_test_app(
            {require_auth: lambda: user, get_url_service: lambda: mock_svc}
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.patch(
                f"/api/v1/urls/{ObjectId()}", json={"status": "INACTIVE"}
            )

        assert resp.status_code == 404

    def test_update_url_invalid_id_returns_400(self):
        user = _make_user()

        application = _build_test_app(
            {require_auth: lambda: user, get_url_service: lambda: AsyncMock()}
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.patch(
                "/api/v1/urls/not-an-objectid", json={"status": "INACTIVE"}
            )

        assert resp.status_code == 422

    def test_update_status_only_filters_other_fields(self):
        """PATCH .../status pre-filters: only status is passed to the service."""
        user = _make_user()
        url_doc = _make_url_doc(owner_id=user.user_id)
        url_doc.status = "INACTIVE"
        url_doc.updated_at = datetime(2024, 6, 1, tzinfo=timezone.utc)

        mock_svc = AsyncMock()
        mock_svc.update = AsyncMock(return_value=url_doc)

        application = _build_test_app(
            {require_auth: lambda: user, get_url_service: lambda: mock_svc}
        )
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.patch(
                f"/api/v1/urls/{ObjectId()}/status",
                json={
                    "status": "INACTIVE",
                    "long_url": "https://should-be-ignored.com",
                },
            )

        assert resp.status_code == 200
        call_args = mock_svc.update.call_args
        update_req = call_args[0][1]
        assert update_req.status == "INACTIVE"
        assert update_req.long_url is None

    def test_delete_url_returns_200_with_message(self):
        user = _make_user()
        mock_svc = AsyncMock()
        mock_svc.delete = AsyncMock(return_value=None)

        application = _build_test_app(
            {require_auth: lambda: user, get_url_service: lambda: mock_svc}
        )
        url_id = str(ObjectId())
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.delete(f"/api/v1/urls/{url_id}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["message"] == "URL deleted"
        assert body["id"] == url_id

    def test_delete_url_forbidden_returns_403(self):
        user = _make_user()
        mock_svc = AsyncMock()
        mock_svc.delete = AsyncMock(side_effect=ForbiddenError("not owner"))

        application = _build_test_app(
            {require_auth: lambda: user, get_url_service: lambda: mock_svc}
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.delete(f"/api/v1/urls/{ObjectId()}")

        assert resp.status_code == 403

    def test_update_url_api_key_missing_scope_returns_403(self):
        key_doc = _make_api_key_doc(scopes=["shorten:create"])  # wrong scope
        user = _make_user(api_key_doc=key_doc)

        application = _build_test_app(
            {require_auth: lambda: user, get_url_service: lambda: AsyncMock()}
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.patch(
                f"/api/v1/urls/{ObjectId()}", json={"status": "INACTIVE"}
            )

        assert resp.status_code == 403

    def test_delete_url_api_key_missing_scope_returns_403(self):
        key_doc = _make_api_key_doc(scopes=["shorten:create"])  # wrong scope
        user = _make_user(api_key_doc=key_doc)

        application = _build_test_app(
            {require_auth: lambda: user, get_url_service: lambda: AsyncMock()}
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.delete(f"/api/v1/urls/{ObjectId()}")

        assert resp.status_code == 403


class TestUpdateUrlWithDomain:
    """`domain` field on PATCH /urls/{id} triggers owner+ACTIVE check at the
    route layer before delegating to the service (Part C cross-tenant move)."""

    def test_update_with_custom_domain_triggers_owner_check(self):
        user = _make_user()
        url_doc = _make_url_doc(owner_id=user.user_id)
        url_doc.domain = "links.acme.com"
        url_doc.updated_at = datetime(2024, 6, 1, tzinfo=timezone.utc)

        url_svc = AsyncMock()
        url_svc.update = AsyncMock(return_value=url_doc)

        custom_svc = AsyncMock()
        custom_svc.assert_owned_and_active = AsyncMock(return_value=None)

        application = _build_test_app(
            {
                require_auth: lambda: user,
                get_url_service: lambda: url_svc,
                get_custom_domain_service: lambda: custom_svc,
            }
        )
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.patch(
                f"/api/v1/urls/{ObjectId()}", json={"domain": "links.acme.com"}
            )

        assert resp.status_code == 200
        # Pin both args — the contract isn't just "fired", it's "fired with
        # the right user + the normalised fqdn".
        assert custom_svc.assert_owned_and_active.await_count == 1
        own_args = custom_svc.assert_owned_and_active.call_args.args
        assert own_args[0].user_id == user.user_id
        assert own_args[1] == "links.acme.com"
        # Service got the move request.
        update_req = url_svc.update.call_args[0][1]
        assert update_req.domain == "links.acme.com"
        # Response carries the new domain so the client can re-render.
        assert resp.json()["domain"] == "links.acme.com"

    def test_update_with_null_domain_skips_owner_check(self):
        """Moving back to the system default doesn't need an ownership check
        — anyone can use the default namespace."""
        user = _make_user()
        url_doc = _make_url_doc(owner_id=user.user_id)
        url_doc.updated_at = datetime(2024, 6, 1, tzinfo=timezone.utc)

        url_svc = AsyncMock()
        url_svc.update = AsyncMock(return_value=url_doc)

        custom_svc = AsyncMock()
        custom_svc.assert_owned_and_active = AsyncMock(return_value=None)

        application = _build_test_app(
            {
                require_auth: lambda: user,
                get_url_service: lambda: url_svc,
                get_custom_domain_service: lambda: custom_svc,
            }
        )
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.patch(f"/api/v1/urls/{ObjectId()}", json={"domain": None})

        assert resp.status_code == 200
        assert custom_svc.assert_owned_and_active.await_count == 0

    def test_update_with_unauthorized_domain_returns_403(self):
        from app.errors import ForbiddenError

        user = _make_user()
        url_svc = AsyncMock()
        url_svc.update = AsyncMock()  # should never fire

        custom_svc = AsyncMock()
        custom_svc.assert_owned_and_active = AsyncMock(
            side_effect=ForbiddenError("You do not own this domain.")
        )

        application = _build_test_app(
            {
                require_auth: lambda: user,
                get_url_service: lambda: url_svc,
                get_custom_domain_service: lambda: custom_svc,
            }
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.patch(
                f"/api/v1/urls/{ObjectId()}", json={"domain": "someone-elses.com"}
            )

        assert resp.status_code == 403
        # Crucially: the service must not have been called. The route guard
        # must reject *before* the move attempt.
        url_svc.update.assert_not_called()


class TestUpdateUrlAbVariants:
    """Setting ab_variants is flag-gated (403 when off); clearing never is."""

    VARIANTS: typing.ClassVar[list[dict]] = [
        {"url": "https://example.com/b", "weight": 40}
    ]

    @staticmethod
    def _flag_svc(enabled: bool) -> AsyncMock:
        svc = AsyncMock()
        svc.is_enabled = AsyncMock(return_value=enabled)
        svc.require = AsyncMock(
            side_effect=None
            if enabled
            else ForbiddenError("A/B testing is not enabled for this account")
        )
        return svc

    def _app(self, user, mock_svc, flag_svc):
        from app.dependencies import get_feature_flag_service

        return _build_test_app(
            {
                require_auth: lambda: user,
                get_url_service: lambda: mock_svc,
                get_feature_flag_service: lambda: flag_svc,
            }
        )

    def test_set_ab_variants_flag_on_returns_200(self):
        user = _make_user()
        url_doc = _make_url_doc(owner_id=user.user_id)
        url_doc.updated_at = datetime(2024, 6, 1, tzinfo=timezone.utc)
        url_doc.ab_variants = [AbVariant(url="https://example.com/b", weight=40)]
        mock_svc = AsyncMock()
        mock_svc.update = AsyncMock(return_value=url_doc)

        application = self._app(user, mock_svc, self._flag_svc(True))
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.patch(
                f"/api/v1/urls/{ObjectId()}", json={"ab_variants": self.VARIANTS}
            )

        assert resp.status_code == 200
        assert resp.json()["ab_variants"] == self.VARIANTS

    def test_set_ab_variants_flag_off_returns_403(self):
        user = _make_user()
        mock_svc = AsyncMock()

        application = self._app(user, mock_svc, self._flag_svc(False))
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.patch(
                f"/api/v1/urls/{ObjectId()}", json={"ab_variants": self.VARIANTS}
            )

        assert resp.status_code == 403
        mock_svc.update.assert_not_called()

    @pytest.mark.parametrize("clear_value", [None, []])
    def test_clear_ab_variants_bypasses_flag(self, clear_value):
        user = _make_user()
        url_doc = _make_url_doc(owner_id=user.user_id)
        url_doc.updated_at = datetime(2024, 6, 1, tzinfo=timezone.utc)
        mock_svc = AsyncMock()
        mock_svc.update = AsyncMock(return_value=url_doc)
        flag_svc = self._flag_svc(False)

        application = self._app(user, mock_svc, flag_svc)
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.patch(
                f"/api/v1/urls/{ObjectId()}", json={"ab_variants": clear_value}
            )

        assert resp.status_code == 200
        flag_svc.require.assert_not_awaited()
        mock_svc.update.assert_called_once()


class TestUpdateUrlGeoRules:
    """Setting geo_rules is flag-gated (403 when off); clearing never is."""

    @staticmethod
    def _flag_svc(enabled: bool) -> AsyncMock:
        svc = AsyncMock()
        svc.is_enabled = AsyncMock(return_value=enabled)
        # Mirror the real require(): no-op when enabled, 403 when not.
        svc.require = AsyncMock(
            side_effect=None
            if enabled
            else ForbiddenError("Geo targeting is not enabled for this account")
        )
        return svc

    def _app(self, user, mock_svc, flag_svc):
        from app.dependencies import get_feature_flag_service

        return _build_test_app(
            {
                require_auth: lambda: user,
                get_url_service: lambda: mock_svc,
                get_feature_flag_service: lambda: flag_svc,
            }
        )

    def test_set_geo_rules_flag_on_returns_200(self):
        user = _make_user()
        url_doc = _make_url_doc(owner_id=user.user_id)
        url_doc.geo_rules = {"IN": "https://example.in/"}
        url_doc.updated_at = datetime(2024, 6, 1, tzinfo=timezone.utc)
        mock_svc = AsyncMock()
        mock_svc.update = AsyncMock(return_value=url_doc)

        application = self._app(user, mock_svc, self._flag_svc(True))
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.patch(
                f"/api/v1/urls/{ObjectId()}",
                json={"geo_rules": {"IN": "https://example.in/"}},
            )

        assert resp.status_code == 200
        assert resp.json()["geo_rules"] == {"IN": "https://example.in/"}

    def test_set_geo_rules_flag_off_returns_403(self):
        user = _make_user()
        mock_svc = AsyncMock()

        application = self._app(user, mock_svc, self._flag_svc(False))
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.patch(
                f"/api/v1/urls/{ObjectId()}",
                json={"geo_rules": {"IN": "https://example.in/"}},
            )

        assert resp.status_code == 403
        mock_svc.update.assert_not_called()

    def test_set_expired_fallback_flag_off_returns_403(self):
        user = _make_user()
        mock_svc = AsyncMock()

        application = self._app(user, mock_svc, self._flag_svc(False))
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.patch(
                f"/api/v1/urls/{ObjectId()}",
                json={"expired_redirect_url": "https://example.com/ended"},
            )

        assert resp.status_code == 403
        mock_svc.update.assert_not_called()

    def test_set_expired_fallback_flag_on_returns_200(self):
        user = _make_user()
        url_doc = _make_url_doc(owner_id=user.user_id)
        url_doc.expired_redirect_url = "https://example.com/ended"
        url_doc.updated_at = datetime(2024, 6, 1, tzinfo=timezone.utc)
        mock_svc = AsyncMock()
        mock_svc.update = AsyncMock(return_value=url_doc)

        application = self._app(user, mock_svc, self._flag_svc(True))
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.patch(
                f"/api/v1/urls/{ObjectId()}",
                json={"expired_redirect_url": "https://example.com/ended"},
            )

        assert resp.status_code == 200
        assert resp.json()["expired_redirect_url"] == "https://example.com/ended"

    def test_clear_expired_fallback_bypasses_flag(self):
        user = _make_user()
        url_doc = _make_url_doc(owner_id=user.user_id)
        url_doc.updated_at = datetime(2024, 6, 1, tzinfo=timezone.utc)
        mock_svc = AsyncMock()
        mock_svc.update = AsyncMock(return_value=url_doc)
        flag_svc = self._flag_svc(False)

        application = self._app(user, mock_svc, flag_svc)
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.patch(
                f"/api/v1/urls/{ObjectId()}", json={"expired_redirect_url": None}
            )

        assert resp.status_code == 200
        flag_svc.require.assert_not_awaited()

    def test_clear_geo_rules_bypasses_flag(self):
        """Rollback posture: a de-allowlisted owner must be able to remove
        their rules, so null-clear never consults the flag."""
        user = _make_user()
        url_doc = _make_url_doc(owner_id=user.user_id)
        url_doc.updated_at = datetime(2024, 6, 1, tzinfo=timezone.utc)
        mock_svc = AsyncMock()
        mock_svc.update = AsyncMock(return_value=url_doc)
        flag_svc = self._flag_svc(False)

        application = self._app(user, mock_svc, flag_svc)
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.patch(f"/api/v1/urls/{ObjectId()}", json={"geo_rules": None})

        assert resp.status_code == 200
        flag_svc.require.assert_not_awaited()
        mock_svc.update.assert_called_once()

    def test_status_endpoint_unaffected_by_flag(self):
        user = _make_user()
        url_doc = _make_url_doc(owner_id=user.user_id)
        url_doc.updated_at = datetime(2024, 6, 1, tzinfo=timezone.utc)
        mock_svc = AsyncMock()
        mock_svc.update = AsyncMock(return_value=url_doc)
        flag_svc = self._flag_svc(False)

        application = self._app(user, mock_svc, flag_svc)
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.patch(
                f"/api/v1/urls/{ObjectId()}/status", json={"status": "INACTIVE"}
            )

        assert resp.status_code == 200
        flag_svc.require.assert_not_awaited()


class TestUpdateUrlScheduling:
    """Setting starts_at / pre_start_url is flag-gated; clearing never is."""

    @staticmethod
    def _flag_svc(enabled: bool) -> AsyncMock:
        svc = AsyncMock()
        svc.require = AsyncMock(
            side_effect=None
            if enabled
            else ForbiddenError("Link scheduling is not enabled for this account")
        )
        return svc

    def _app(self, user, mock_svc, flag_svc):
        from app.dependencies import get_feature_flag_service

        return _build_test_app(
            {
                require_auth: lambda: user,
                get_url_service: lambda: mock_svc,
                get_feature_flag_service: lambda: flag_svc,
            }
        )

    def test_set_start_flag_on_echoes_fields(self):
        user = _make_user()
        url_doc = _make_url_doc(owner_id=user.user_id)
        url_doc.starts_at = datetime(2030, 1, 1, tzinfo=timezone.utc)
        url_doc.pre_start_url = "https://example.org/soon"
        url_doc.updated_at = datetime(2024, 6, 1, tzinfo=timezone.utc)
        mock_svc = AsyncMock()
        mock_svc.update = AsyncMock(return_value=url_doc)

        application = self._app(user, mock_svc, self._flag_svc(True))
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.patch(
                f"/api/v1/urls/{ObjectId()}",
                json={
                    "starts_at": 1893456000,
                    "pre_start_url": "https://example.org/soon",
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["starts_at"] == 1893456000
        assert body["pre_start_url"] == "https://example.org/soon"
        assert body["status"] == "SCHEDULED"

    def test_set_start_flag_off_returns_403(self):
        user = _make_user()
        mock_svc = AsyncMock()
        application = self._app(user, mock_svc, self._flag_svc(False))
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.patch(
                f"/api/v1/urls/{ObjectId()}", json={"starts_at": 1893456000}
            )

        assert resp.status_code == 403
        mock_svc.update.assert_not_called()

    def test_clear_start_bypasses_flag(self):
        user = _make_user()
        url_doc = _make_url_doc(owner_id=user.user_id)
        url_doc.updated_at = datetime(2024, 6, 1, tzinfo=timezone.utc)
        mock_svc = AsyncMock()
        mock_svc.update = AsyncMock(return_value=url_doc)
        flag_svc = self._flag_svc(False)

        application = self._app(user, mock_svc, flag_svc)
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.patch(
                f"/api/v1/urls/{ObjectId()}",
                json={"starts_at": None, "pre_start_url": None},
            )

        assert resp.status_code == 200
        flag_svc.require.assert_not_awaited()
