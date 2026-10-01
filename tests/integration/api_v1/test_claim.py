"""Integration tests for POST /api/v1/urls/claim."""

from __future__ import annotations

from unittest.mock import AsyncMock

from bson import ObjectId
from fastapi.testclient import TestClient

from app.dependencies import get_current_user, get_url_service, require_auth
from app.services.url_service import ClaimResult

from .conftest import _build_test_app, _make_user

VALID_ID = "507f1f77bcf86cd799439011"
TOKEN = "x" * 43


def _post(client: TestClient, claims: list[dict]):
    return client.post("/api/v1/urls/claim", json={"claims": claims})


def _client(mock_svc, user) -> TestClient:
    application = _build_test_app(
        {require_auth: lambda: user, get_url_service: lambda: mock_svc}
    )
    return TestClient(application, raise_server_exceptions=False)


class TestClaimUrls:
    def test_requires_auth(self):
        application = _build_test_app(
            {get_current_user: lambda: None, get_url_service: lambda: AsyncMock()}
        )
        client = TestClient(application, raise_server_exceptions=False)
        resp = _post(client, [{"url_id": VALID_ID, "token": TOKEN}])
        assert resp.status_code == 401

    def test_happy_path_response_shape(self):
        user = _make_user()
        mock_svc = AsyncMock()
        mock_svc.claim = AsyncMock(return_value=[ClaimResult(VALID_ID, "claimed")])

        resp = _post(_client(mock_svc, user), [{"url_id": VALID_ID, "token": TOKEN}])

        assert resp.status_code == 200
        assert resp.json() == {
            "results": [{"url_id": VALID_ID, "status": "claimed"}],
            "claimed": 1,
        }
        # Service receives the parsed items and the caller's user id.
        args = mock_svc.claim.call_args.args
        assert args[0][0].url_id == VALID_ID
        assert args[0][0].token == TOKEN
        assert args[1] == user.user_id

    def test_mixed_results_counted_and_ordered(self):
        ids = [str(ObjectId()) for _ in range(3)]
        user = _make_user()
        mock_svc = AsyncMock()
        mock_svc.claim = AsyncMock(
            return_value=[
                ClaimResult(ids[0], "invalid"),
                ClaimResult(ids[1], "already_yours"),
                ClaimResult(ids[2], "claimed"),
            ]
        )

        resp = _post(
            _client(mock_svc, user),
            [{"url_id": i, "token": TOKEN} for i in ids],
        )

        body = resp.json()
        assert [r["status"] for r in body["results"]] == [
            "invalid",
            "already_yours",
            "claimed",
        ]
        assert body["claimed"] == 1

    def test_empty_batch_is_422(self):
        resp = _post(_client(AsyncMock(), _make_user()), [])
        assert resp.status_code == 422

    def test_oversize_batch_is_422(self):
        claims = [{"url_id": str(ObjectId()), "token": TOKEN} for _ in range(17)]
        resp = _post(_client(AsyncMock(), _make_user()), claims)
        assert resp.status_code == 422

    def test_malformed_url_id_is_422(self):
        resp = _post(
            _client(AsyncMock(), _make_user()),
            [{"url_id": "not-an-objectid!", "token": TOKEN}],
        )
        assert resp.status_code == 422

    def test_short_token_is_422(self):
        resp = _post(
            _client(AsyncMock(), _make_user()),
            [{"url_id": VALID_ID, "token": "short"}],
        )
        assert resp.status_code == 422
