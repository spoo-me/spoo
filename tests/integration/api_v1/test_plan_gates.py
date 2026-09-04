"""One route test per plan-gated field: a free account posting the field gets
403 ``plan_required`` naming the feature, on create and on update."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

from dependencies import (
    get_current_user,
    get_entitlements,
    get_feature_flag_service,
    get_url_service,
    require_auth,
)
from services.entitlements import for_plan
from services.feature_flag_service import FeatureFlagService
from services.features.catalog import Plan

from .conftest import _build_test_app, _make_user

GATED_FIELDS = [
    ("geo_rules", {"IN": "https://example.in/"}, "geo_targeting"),
    ("ab_variants", [{"url": "https://example.com/b", "weight": 40}], "ab_variants"),
    ("expired_redirect_url", "https://example.com/gone", "expired_fallback"),
    ("meta_tags", {"title": "Launch"}, "custom_meta_tags"),
    ("starts_at", "2030-01-01T00:00:00Z", "link_scheduling"),
]


def _flag_svc() -> FeatureFlagService:
    """Real ``require`` with every flag rolled out, so only the plan gate decides."""
    svc = FeatureFlagService.__new__(FeatureFlagService)

    async def _is_enabled(name, user):
        return True

    svc.is_enabled = _is_enabled  # type: ignore[method-assign]
    return svc


def _client(plan: Plan) -> tuple[TestClient, AsyncMock]:
    user = _make_user()
    url_svc = AsyncMock()
    app = _build_test_app(
        {
            get_current_user: lambda: user,
            require_auth: lambda: user,
            get_url_service: lambda: url_svc,
            get_feature_flag_service: _flag_svc,
            get_entitlements: lambda: for_plan(plan),
        }
    )
    return TestClient(app, raise_server_exceptions=False), url_svc


@pytest.mark.parametrize(
    ("field", "value", "feature"), GATED_FIELDS, ids=lambda v: str(v)[:24]
)
def test_free_account_cannot_shorten_with_the_field(field, value, feature):
    client, url_svc = _client(Plan.FREE)
    with client:
        resp = client.post(
            "/api/v1/shorten", json={"long_url": "https://example.com", field: value}
        )
    assert resp.status_code == 403
    assert resp.json()["code"] == "plan_required"
    assert resp.json()["feature"] == feature
    url_svc.create.assert_not_awaited()


@pytest.mark.parametrize(
    ("field", "value", "feature"), GATED_FIELDS, ids=lambda v: str(v)[:24]
)
def test_free_account_cannot_update_with_the_field(field, value, feature):
    client, url_svc = _client(Plan.FREE)
    with client:
        resp = client.patch(f"/api/v1/urls/{ObjectId()}", json={field: value})
    assert resp.status_code == 403
    assert resp.json()["code"] == "plan_required"
    assert resp.json()["feature"] == feature
    url_svc.update.assert_not_awaited()


@pytest.mark.parametrize(
    ("field", "value", "feature"), GATED_FIELDS, ids=lambda v: str(v)[:24]
)
def test_pro_account_passes_the_gate(field, value, feature):
    client, _ = _client(Plan.PRO)
    with client:
        resp = client.post(
            "/api/v1/shorten", json={"long_url": "https://example.com", field: value}
        )
    assert resp.status_code != 403, resp.json()
