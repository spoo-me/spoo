"""GET /api/v1/me/features — the features map: flag state joined with the plan."""

from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from dependencies import (
    get_current_user,
    get_entitlements,
    get_feature_flag_service,
    require_auth,
    require_jwt,
)
from services.entitlements import for_plan
from services.feature_flag_service import (
    AB_TESTING_FLAG,
    CUSTOM_DOMAINS_FLAG,
    EXPOSED_FEATURES,
    GEO_TARGETING_FLAG,
    META_TAGS_FLAG,
    WEBHOOKS_FLAG,
    FeatureFlagService,
)
from services.features.catalog import Plan

from .conftest import _build_test_app, _make_api_key_doc, _make_user


def _flag_svc(enabled_names: set[str]) -> FeatureFlagService:
    """A real service instance with only ``is_enabled`` faked, so the test
    exercises the real ``states_for`` join (hidden, locked, enabled)."""
    svc = FeatureFlagService.__new__(FeatureFlagService)

    async def _is_enabled(name: str, user) -> bool:
        return name in enabled_names

    svc.is_enabled = _is_enabled  # type: ignore[method-assign]
    return svc


def _app(user, enabled_names: set[str], plan: Plan = Plan.FREE):
    return _build_test_app(
        {
            require_jwt: lambda: user,
            get_current_user: lambda: user,
            get_feature_flag_service: lambda: _flag_svc(enabled_names),
            get_entitlements: lambda: for_plan(plan),
        }
    )


def test_requires_auth():
    # No auth override at all: the real require_jwt runs and rejects.
    app = _build_test_app({get_feature_flag_service: lambda: AsyncMock()})
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/v1/me/features")
    assert resp.status_code == 401


def test_api_key_rejected():
    # /me/* is JWT-only: a request that authenticates with a valid API key
    # (any scope) must get 403 from the real require_jwt, not slip through
    # as a session. Pins the AuthUser → JwtUser distinction.
    user = _make_user(api_key_doc=_make_api_key_doc())
    app = _build_test_app(
        {
            require_auth: lambda: user,
            get_feature_flag_service: lambda: AsyncMock(),
        }
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/v1/me/features")
    assert resp.status_code == 403


def test_all_hidden_when_nothing_is_rolled_out():
    user = _make_user()
    with TestClient(_app(user, set(), Plan.PRO)) as client:
        resp = client.get("/api/v1/me/features")
    assert resp.status_code == 200
    features = resp.json()["features"]
    assert set(features) == set(EXPOSED_FEATURES)
    assert set(features.values()) == {"hidden"}


def test_rolled_out_but_not_entitled_is_locked():
    user = _make_user()
    enabled = {GEO_TARGETING_FLAG, CUSTOM_DOMAINS_FLAG, WEBHOOKS_FLAG}
    with TestClient(_app(user, enabled, Plan.FREE)) as client:
        resp = client.get("/api/v1/me/features")
    features = resp.json()["features"]
    assert features[GEO_TARGETING_FLAG] == "locked"
    assert features[CUSTOM_DOMAINS_FLAG] == "locked"
    # Free holds webhooks, so a rolled-out flag reads enabled.
    assert features[WEBHOOKS_FLAG] == "enabled"
    assert features[META_TAGS_FLAG] == "hidden"
    assert features[AB_TESTING_FLAG] == "hidden"


def test_rolled_out_and_entitled_is_enabled():
    user = _make_user()
    enabled = {GEO_TARGETING_FLAG, CUSTOM_DOMAINS_FLAG}
    with TestClient(_app(user, enabled, Plan.PRO)) as client:
        resp = client.get("/api/v1/me/features")
    features = resp.json()["features"]
    assert features[GEO_TARGETING_FLAG] == "enabled"
    assert features[CUSTOM_DOMAINS_FLAG] == "enabled"
    assert features[META_TAGS_FLAG] == "hidden"


def test_response_covers_every_exposed_feature():
    # The contract clients rely on: the map always carries the full catalog,
    # so a frontend can treat "missing" as hidden without ever hitting it.
    user = _make_user()
    with TestClient(_app(user, set(EXPOSED_FEATURES), Plan.PRO)) as client:
        resp = client.get("/api/v1/me/features")
    features = resp.json()["features"]
    assert set(features) == set(EXPOSED_FEATURES)
    assert set(features.values()) == {"enabled"}
