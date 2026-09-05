"""GET /api/v1/plans — public catalog projection with display prices."""

from __future__ import annotations

import os
from unittest.mock import patch

from fastapi.testclient import TestClient

from services.features.catalog import Plan, bool_features, int_features, plan_defaults

from .conftest import _build_test_app

_PADDLE = {
    "BILLING_PROVIDER": "paddle",
    "BILLING_PADDLE_API_KEY": "pdl_sdbx_test",
    "BILLING_PADDLE_WEBHOOK_SECRET": "secret",
    "BILLING_PADDLE_PRICE_PRO_MONTHLY": "pri_m",
    "BILLING_PADDLE_PRICE_PRO_YEAR": "pri_y",
}


def test_public_and_shaped_from_the_catalog():
    with TestClient(_build_test_app({})) as client:
        resp = client.get("/api/v1/plans")
    assert resp.status_code == 200
    body = resp.json()
    assert [p["name"] for p in body["plans"]] == ["free", "pro"]
    free, pro = body["plans"]
    assert set(free["features"]) == {f.key for f in bool_features()}
    assert set(free["limits"]) == {f.key for f in int_features()}
    assert pro["features"]["geo_targeting"] is True
    assert free["features"]["geo_targeting"] is False
    assert (
        pro["limits"]["custom_domains_max"]
        == plan_defaults(Plan.PRO)["custom_domains_max"]
    )
    assert free["limits"]["webhook_endpoints_max"] == 1


def test_prices_come_from_config():
    with patch.dict(
        os.environ,
        {
            **_PADDLE,
            "BILLING_PRO_MONTHLY_USD": "20",
            "BILLING_PRO_YEAR_USD": "200",
            "BILLING_FOUNDING_MONTHLY_USD": "11",
        },
    ):
        app = _build_test_app({})
    with TestClient(app) as client:
        body = client.get("/api/v1/plans").json()
    assert body["prices"]["monthly"] == {"amount": 20, "currency": "USD"}
    assert body["prices"]["year"] == {"amount": 200, "currency": "USD"}
    assert body["founding"]["monthly"]["amount"] == 11
    assert body["founding"]["seats_total"] == 100
    assert body["founding"]["seats_left"] is None
    assert body["founding"]["until"] is None


def test_default_prices_are_the_locked_ones():
    with patch.dict(os.environ, _PADDLE):
        app = _build_test_app({})
    with TestClient(app) as client:
        body = client.get("/api/v1/plans").json()
    assert body["prices"]["monthly"]["amount"] == 15
    assert body["prices"]["year"]["amount"] == 144
    assert body["founding"]["monthly"]["amount"] == 9
    assert body["founding"]["year"]["amount"] == 90


def test_selfhost_has_nothing_to_sell():
    with patch.dict(os.environ, {"BILLING_PROVIDER": "none"}):
        app = _build_test_app({})
    with TestClient(app) as client:
        body = client.get("/api/v1/plans").json()
    assert [p["name"] for p in body["plans"]] == ["free", "pro"]
    assert body["prices"] == {}
    assert body["founding"] is None
