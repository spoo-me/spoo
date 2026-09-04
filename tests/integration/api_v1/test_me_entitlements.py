"""GET /api/v1/me/entitlements — plan block, features, limits with live usage,
version; and the X-Entitlements-Version header contract."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from dependencies import (
    get_current_user,
    get_entitlement_service,
    get_entitlements,
    get_feature_flag_service,
    require_jwt,
)
from schemas.models.subscription import SubscriptionStatus
from services.entitlements import Resolved, for_plan
from services.features.catalog import Plan, int_features, plan_defaults

from .conftest import _build_test_app, _make_user
from .test_me_features import _flag_svc


def _app(user, resolved: Resolved, *, enabled: set[str] = frozenset(), used=None):
    ent_service = AsyncMock()
    ent_service.usage_for = AsyncMock(return_value=used or {})
    ent_service.over_limit_for = AsyncMock(return_value={})
    return _build_test_app(
        {
            require_jwt: lambda: user,
            get_current_user: lambda: user,
            get_feature_flag_service: lambda: _flag_svc(set(enabled)),
            get_entitlements: lambda: resolved,
            get_entitlement_service: lambda: ent_service,
        }
    )


def test_requires_auth():
    app = _build_test_app({})
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/v1/me/entitlements")
    assert resp.status_code == 401


def test_free_shape():
    user = _make_user()
    resolved = for_plan(Plan.FREE, version=0)
    with TestClient(_app(user, resolved, enabled={"geo_targeting"})) as client:
        resp = client.get("/api/v1/me/entitlements")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == 0
    assert body["plan"] == {
        "name": "free",
        "status": None,
        "until": None,
        "founding": False,
        "renews": False,
    }
    assert body["features"]["geo_targeting"] == "locked"
    assert set(body["limits"]) == {f.key for f in int_features()}
    assert body["limits"]["custom_domains_max"] == {"max": 0, "used": None}
    assert body["limits"]["webhook_endpoints_max"]["max"] == 1
    assert body["over_limit"] == {}


def test_pro_in_grace_carries_status_date_and_live_usage():
    user = _make_user()
    until = datetime(2026, 10, 1, tzinfo=timezone.utc)
    resolved = Resolved(
        plan=Plan.PRO,
        status=SubscriptionStatus.GRACE,
        until=until,
        founding=True,
        values=plan_defaults(Plan.PRO),
        version=7,
    )
    used = {"custom_domains_max": 2, "webhook_endpoints_max": 3, "api_keys_max": 1}
    with TestClient(
        _app(user, resolved, enabled={"geo_targeting", "custom_domains"}, used=used)
    ) as client:
        resp = client.get("/api/v1/me/entitlements")
    body = resp.json()
    assert body["version"] == 7
    assert body["plan"]["name"] == "pro"
    assert body["plan"]["status"] == "grace"
    assert body["plan"]["until"] == "2026-10-01T00:00:00+00:00"
    assert body["plan"]["founding"] is True
    assert body["features"]["geo_targeting"] == "enabled"
    assert body["features"]["custom_domains"] == "enabled"
    assert body["features"]["ab_variants"] == "hidden"
    assert body["limits"]["custom_domains_max"] == {"max": 5, "used": 2}
    assert body["limits"]["webhook_endpoints_max"] == {"max": 10, "used": 3}
    assert body["limits"]["analytics_window_days"] == {"max": 730, "used": None}


def test_version_header_is_set_from_the_dependency():
    """Through the real dependency: the service resolves, the dependency
    stores the version, the middleware emits it."""
    from middleware.entitlements import HEADER, EntitlementsVersionMiddleware

    user = _make_user()
    resolved = for_plan(Plan.PRO, version=5)
    ent_service = AsyncMock()
    ent_service.resolve_for = AsyncMock(return_value=resolved)
    ent_service.usage_for = AsyncMock(return_value={})
    ent_service.over_limit_for = AsyncMock(return_value={})
    app = _build_test_app(
        {
            require_jwt: lambda: user,
            get_current_user: lambda: user,
            get_feature_flag_service: lambda: _flag_svc(set()),
            get_entitlement_service: lambda: ent_service,
        }
    )
    app.add_middleware(EntitlementsVersionMiddleware)
    with TestClient(app) as client:
        resp = client.get("/api/v1/me/entitlements")
    assert resp.status_code == 200
    assert resp.headers[HEADER] == "5"
    ent_service.resolve_for.assert_awaited_once_with(user.user_id, plan_hint=None)


def test_dependency_passes_the_jwt_plan_hint():
    user = _make_user()
    user.plan_claim = "pro"
    ent_service = AsyncMock()
    ent_service.resolve_for = AsyncMock(return_value=for_plan(Plan.FREE))
    ent_service.usage_for = AsyncMock(return_value={})
    ent_service.over_limit_for = AsyncMock(return_value={})
    app = _build_test_app(
        {
            require_jwt: lambda: user,
            get_current_user: lambda: user,
            get_feature_flag_service: lambda: _flag_svc(set()),
            get_entitlement_service: lambda: ent_service,
        }
    )
    with TestClient(app) as client:
        client.get("/api/v1/me/entitlements")
    ent_service.resolve_for.assert_awaited_once_with(user.user_id, plan_hint="pro")


def test_until_is_the_prepaid_end_for_an_active_prepaid():
    user = _make_user()
    end = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=300)
    resolved = Resolved(
        plan=Plan.PRO,
        status=SubscriptionStatus.ACTIVE,
        until=end,
        values=plan_defaults(Plan.PRO),
        version=1,
    )
    with TestClient(_app(user, resolved)) as client:
        body = client.get("/api/v1/me/entitlements").json()
    assert body["plan"]["until"] == end.isoformat()


def test_over_limit_lists_paused_items_per_limit():
    user = _make_user()
    resolved = for_plan(Plan.FREE, version=4)
    ent_service = AsyncMock()
    ent_service.usage_for = AsyncMock(return_value={"webhook_endpoints_max": 3})
    ent_service.over_limit_for = AsyncMock(
        return_value={"webhook_endpoints_max": ["aaa", "bbb"]}
    )
    app = _build_test_app(
        {
            require_jwt: lambda: user,
            get_current_user: lambda: user,
            get_feature_flag_service: lambda: _flag_svc(set()),
            get_entitlements: lambda: resolved,
            get_entitlement_service: lambda: ent_service,
        }
    )
    with TestClient(app) as client:
        body = client.get("/api/v1/me/entitlements").json()
    assert body["over_limit"] == {"webhook_endpoints_max": {"paused": ["aaa", "bbb"]}}
    assert body["limits"]["webhook_endpoints_max"] == {"max": 1, "used": 3}


def _onboarding_client(plan: Plan) -> tuple[TestClient, AsyncMock]:
    from dependencies import get_user_repo

    user = _make_user()
    repo = AsyncMock()
    repo.mark_pro_onboarded = AsyncMock(return_value=True)
    app = _build_test_app(
        {
            require_jwt: lambda: user,
            get_current_user: lambda: user,
            get_entitlements: lambda: for_plan(plan),
            get_user_repo: lambda: repo,
        }
    )
    return TestClient(app), repo


def test_pro_account_marks_the_tour_seen():
    client, repo = _onboarding_client(Plan.PRO)
    with client:
        resp = client.post("/api/v1/me/pro-onboarding")
    assert resp.status_code == 204
    assert resp.content == b""
    repo.mark_pro_onboarded.assert_awaited_once()


def test_free_account_cannot_stamp_the_pro_tour():
    client, repo = _onboarding_client(Plan.FREE)
    with client:
        resp = client.post("/api/v1/me/pro-onboarding")
    assert resp.status_code == 403
    assert resp.json()["code"] == "forbidden"
    repo.mark_pro_onboarded.assert_not_awaited()


def test_selfhost_account_marks_the_tour_seen():
    client, repo = _onboarding_client(Plan.SELFHOST)
    with client:
        assert client.post("/api/v1/me/pro-onboarding").status_code == 204
    repo.mark_pro_onboarded.assert_awaited_once()
