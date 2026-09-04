"""Tests for POST /api/v1/urls/bulk/{delete,status,expiry}.

Route-layer concerns only: envelope validation, auth/scope, and the
passthrough to BulkUrlService. Per-item semantics live in
tests/unit/services/test_bulk_url_service.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from bson import ObjectId
from fastapi.testclient import TestClient

from dependencies import get_bulk_url_service, require_auth
from errors import ForbiddenError, ValidationError
from schemas.dto.responses.bulk import (
    BulkOperationSummary,
    BulkUrlOperationResponse,
    BulkUrlResultRow,
)

from .conftest import _build_test_app, _make_api_key_doc, _make_user

VALID_ID = "665f0c2f9e7a4b1d2c3d4e5f"


def _report(url_id: str = VALID_ID) -> BulkUrlOperationResponse:
    return BulkUrlOperationResponse(
        summary=BulkOperationSummary(total=1, succeeded=1, failed=0),
        results=[BulkUrlResultRow(id=url_id, alias="promo", ok=True)],
    )


def _client(mock_svc, user=None) -> TestClient:
    overrides = {get_bulk_url_service: lambda: mock_svc}
    if user is not None:
        overrides[require_auth] = lambda: user
    application = _build_test_app(overrides)
    return TestClient(application, raise_server_exceptions=True)


class TestBulkDeleteRoute:
    def test_delete_passes_object_ids_and_owner(self):
        user = _make_user()
        mock_svc = AsyncMock()
        mock_svc.bulk_delete = AsyncMock(return_value=_report())

        with _client(mock_svc, user) as client:
            resp = client.post("/api/v1/urls/bulk/delete", json={"ids": [VALID_ID]})

        assert resp.status_code == 200
        body = resp.json()
        assert body["summary"] == {"total": 1, "succeeded": 1, "failed": 0}
        assert body["results"][0]["ok"] is True
        mock_svc.bulk_delete.assert_awaited_once_with(
            [ObjectId(VALID_ID)], user.user_id
        )

    def test_empty_ids_is_422_envelope_rejection(self):
        user = _make_user()
        mock_svc = AsyncMock()
        with _client(mock_svc, user) as client:
            resp = client.post("/api/v1/urls/bulk/delete", json={"ids": []})
        assert resp.status_code == 422
        mock_svc.bulk_delete.assert_not_awaited()

    def test_over_plan_batch_is_403_limit_reached(self):
        # The free plan's bulk_batch_max is 100; the DTO cap is the pro max.
        user = _make_user()
        mock_svc = AsyncMock()
        ids = [f"{i:024x}" for i in range(101)]
        with _client(mock_svc, user) as client:
            resp = client.post("/api/v1/urls/bulk/delete", json={"ids": ids})
        assert resp.status_code == 403
        body = resp.json()
        assert body["code"] == "limit_reached"
        assert body["limit"] == "bulk_batch_max"
        assert body["max"] == 100
        assert body["current"] == 101
        mock_svc.bulk_delete.assert_not_awaited()

    def test_over_dto_cap_is_422(self):
        user = _make_user()
        mock_svc = AsyncMock()
        ids = [f"{i:024x}" for i in range(1001)]
        with _client(mock_svc, user) as client:
            resp = client.post("/api/v1/urls/bulk/delete", json={"ids": ids})
        assert resp.status_code == 422
        mock_svc.bulk_delete.assert_not_awaited()

    def test_malformed_id_is_422(self):
        user = _make_user()
        mock_svc = AsyncMock()
        with _client(mock_svc, user) as client:
            resp = client.post(
                "/api/v1/urls/bulk/delete", json={"ids": [VALID_ID, "nope"]}
            )
        assert resp.status_code == 422
        mock_svc.bulk_delete.assert_not_awaited()

    def test_requires_auth(self):
        with _client(AsyncMock()) as client:
            resp = client.post("/api/v1/urls/bulk/delete", json={"ids": [VALID_ID]})
        assert resp.status_code == 401

    def test_api_key_without_scope_is_403(self):
        key = _make_api_key_doc(scopes=["urls:read"])
        user = _make_user(api_key_doc=key)
        with _client(AsyncMock(), user) as client:
            resp = client.post("/api/v1/urls/bulk/delete", json={"ids": [VALID_ID]})
        assert resp.status_code == 403

    def test_api_key_with_manage_scope_allowed(self):
        key = _make_api_key_doc(scopes=["urls:manage"])
        user = _make_user(api_key_doc=key)
        mock_svc = AsyncMock()
        mock_svc.bulk_delete = AsyncMock(return_value=_report())
        with _client(mock_svc, user) as client:
            resp = client.post("/api/v1/urls/bulk/delete", json={"ids": [VALID_ID]})
        assert resp.status_code == 200


class TestBulkStatusRoute:
    def test_status_passes_literal_through(self):
        user = _make_user()
        mock_svc = AsyncMock()
        mock_svc.bulk_set_status = AsyncMock(return_value=_report())
        with _client(mock_svc, user) as client:
            resp = client.post(
                "/api/v1/urls/bulk/status",
                json={"ids": [VALID_ID], "status": "INACTIVE"},
            )
        assert resp.status_code == 200
        args = mock_svc.bulk_set_status.await_args.args
        assert args[0] == [ObjectId(VALID_ID)]
        assert args[1] == "INACTIVE"

    def test_blocked_status_is_422(self):
        user = _make_user()
        with _client(AsyncMock(), user) as client:
            resp = client.post(
                "/api/v1/urls/bulk/status",
                json={"ids": [VALID_ID], "status": "BLOCKED"},
            )
        assert resp.status_code == 422

    def test_missing_status_is_422(self):
        user = _make_user()
        with _client(AsyncMock(), user) as client:
            resp = client.post("/api/v1/urls/bulk/status", json={"ids": [VALID_ID]})
        assert resp.status_code == 422


class TestBulkExpiryRoute:
    def test_expiry_epoch_coerced_and_passed(self):
        user = _make_user()
        mock_svc = AsyncMock()
        mock_svc.bulk_set_expiry = AsyncMock(return_value=_report())
        with _client(mock_svc, user) as client:
            resp = client.post(
                "/api/v1/urls/bulk/expiry",
                json={"ids": [VALID_ID], "expire_after": 4102444800},
            )
        assert resp.status_code == 200
        args = mock_svc.bulk_set_expiry.await_args.args
        assert args[1].year == 2100

    def test_expiry_null_clears(self):
        user = _make_user()
        mock_svc = AsyncMock()
        mock_svc.bulk_set_expiry = AsyncMock(return_value=_report())
        with _client(mock_svc, user) as client:
            resp = client.post(
                "/api/v1/urls/bulk/expiry",
                json={"ids": [VALID_ID], "expire_after": None},
            )
        assert resp.status_code == 200
        assert mock_svc.bulk_set_expiry.await_args.args[1] is None

    def test_expiry_field_required(self):
        user = _make_user()
        with _client(AsyncMock(), user) as client:
            resp = client.post("/api/v1/urls/bulk/expiry", json={"ids": [VALID_ID]})
        assert resp.status_code == 422

    def test_past_expiry_is_400_app_error_envelope(self):
        """The past-value check is service-side (envelope-level AppError),
        surfacing as the standard 400 body — not a per-item report."""
        user = _make_user()
        mock_svc = AsyncMock()
        mock_svc.bulk_set_expiry = AsyncMock(
            side_effect=ValidationError(
                "expire_after must be in the future", field="expire_after"
            )
        )
        with _client(mock_svc, user) as client:
            resp = client.post(
                "/api/v1/urls/bulk/expiry",
                json={"ids": [VALID_ID], "expire_after": 1000000000},
            )
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "validation_error"
        assert body["field"] == "expire_after"


class TestBulkDomainRoute:
    def test_custom_target_triggers_owner_check_and_passes_through(self):
        user = _make_user()
        mock_svc = AsyncMock()
        mock_svc.bulk_move_domain = AsyncMock(return_value=_report())
        custom_svc = AsyncMock()
        custom_svc.assert_owned_and_active = AsyncMock(return_value=None)

        from dependencies import get_custom_domain_service

        application = _build_test_app(
            {
                require_auth: lambda: user,
                get_bulk_url_service: lambda: mock_svc,
                get_custom_domain_service: lambda: custom_svc,
            }
        )
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.post(
                "/api/v1/urls/bulk/domain",
                json={"ids": [VALID_ID], "domain": "Links.ACME.com"},
            )

        assert resp.status_code == 200
        # Ownership fired once, with the normalised fqdn.
        own_args = custom_svc.assert_owned_and_active.call_args.args
        assert own_args[0].user_id == user.user_id
        assert own_args[1] == "links.acme.com"
        args = mock_svc.bulk_move_domain.await_args.args
        assert args[0] == [ObjectId(VALID_ID)]
        assert args[1] == "links.acme.com"

    def test_null_target_skips_owner_check(self):
        user = _make_user()
        mock_svc = AsyncMock()
        mock_svc.bulk_move_domain = AsyncMock(return_value=_report())
        custom_svc = AsyncMock()
        custom_svc.assert_owned_and_active = AsyncMock()

        from dependencies import get_custom_domain_service

        application = _build_test_app(
            {
                require_auth: lambda: user,
                get_bulk_url_service: lambda: mock_svc,
                get_custom_domain_service: lambda: custom_svc,
            }
        )
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.post(
                "/api/v1/urls/bulk/domain",
                json={"ids": [VALID_ID], "domain": None},
            )

        assert resp.status_code == 200
        custom_svc.assert_owned_and_active.assert_not_awaited()
        assert mock_svc.bulk_move_domain.await_args.args[1] is None

    def test_unowned_target_rejects_envelope(self):
        """A bad target fails the whole request before any item is
        attempted — zero rows, standard AppError body."""
        user = _make_user()
        mock_svc = AsyncMock()
        custom_svc = AsyncMock()
        custom_svc.assert_owned_and_active = AsyncMock(
            side_effect=ForbiddenError("You don't own this domain")
        )

        from dependencies import get_custom_domain_service

        application = _build_test_app(
            {
                require_auth: lambda: user,
                get_bulk_url_service: lambda: mock_svc,
                get_custom_domain_service: lambda: custom_svc,
            }
        )
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.post(
                "/api/v1/urls/bulk/domain",
                json={"ids": [VALID_ID], "domain": "links.acme.com"},
            )

        assert resp.status_code == 403
        assert resp.json()["code"] == "forbidden"
        mock_svc.bulk_move_domain.assert_not_awaited()

    def test_missing_domain_field_is_422(self):
        user = _make_user()
        with _client(AsyncMock(), user) as client:
            resp = client.post("/api/v1/urls/bulk/domain", json={"ids": [VALID_ID]})
        assert resp.status_code == 422
