"""Tests for GET /api/v1/expand (redirect-chain expander)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.dependencies import get_current_user
from app.infrastructure.cache.meta_fetch_cache import MetaFetchCache
from app.infrastructure.safe_fetch import ChainHop, ExpandedChain, FetchHardError
from app.infrastructure.web_risk import DISPLAY_THREAT_TYPES, WebRiskClient
from app.services.url_expand_service import UrlExpandService

from .conftest import _build_test_app

CHAIN = ExpandedChain(
    hops=[
        ChainHop("https://bit.ly/x", 301),
        ChainHop("http://tracker.example/r", 302),
        ChainHop("https://dest.example/page", 200),
    ],
    final_url="https://dest.example/page",
    final_status=200,
    truncated=False,
)


def _service(patterns=None, *, web_risk_key="", http=None, cache=None, budget=None):
    repo = AsyncMock()
    repo.get_patterns = AsyncMock(return_value=patterns or [])
    return UrlExpandService(
        repo,
        cache or MetaFetchCache(None),
        regex_timeout=0.2,
        user_agent="test",
        web_risk=(
            WebRiskClient(
                http or MagicMock(get=AsyncMock()),
                api_key=web_risk_key,
                threat_types=DISPLAY_THREAT_TYPES,
            )
            if web_risk_key
            else None
        ),
        web_risk_budget=budget,
    )


def _app(patterns=None, **kwargs):
    app = _build_test_app({get_current_user: lambda: None})
    app.state.url_expand_service = _service(patterns, **kwargs)
    return app


def test_expand_returns_chain_anonymously():
    with (
        patch(
            "app.services.url_expand_service.expand_public",
            new=AsyncMock(return_value=CHAIN),
        ),
        TestClient(_app(), raise_server_exceptions=True) as client,
    ):
        resp = client.get("/api/v1/expand", params={"url": "https://bit.ly/x"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["final_url"] == "https://dest.example/page"
    assert [h["status"] for h in data["hops"]] == [301, 302, 200]
    assert data["hops"][1]["https"] is False
    assert data["blocklist_match"] is False
    assert data["truncated"] is False
    assert data["web_risk"] is None


def test_expand_flags_blocklisted_hop():
    with (
        patch(
            "app.services.url_expand_service.expand_public",
            new=AsyncMock(return_value=CHAIN),
        ),
        TestClient(
            _app(patterns=[r"tracker\.example"]), raise_server_exceptions=True
        ) as client,
    ):
        resp = client.get("/api/v1/expand", params={"url": "https://bit.ly/x"})
    assert resp.json()["blocklist_match"] is True


def test_expand_rejects_non_http_scheme():
    with TestClient(_app(), raise_server_exceptions=False) as client:
        resp = client.get("/api/v1/expand", params={"url": "ftp://dest.example"})
    assert resp.status_code == 400


def test_expand_unreachable_is_422():
    with (
        patch(
            "app.services.url_expand_service.expand_public",
            new=AsyncMock(
                side_effect=FetchHardError("resolves to a non-public address")
            ),
        ),
        TestClient(_app(), raise_server_exceptions=False) as client,
    ):
        resp = client.get("/api/v1/expand", params={"url": "https://internal.example"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "unfetchable"


def test_web_risk_verdict_rides_the_wire():
    http = MagicMock()
    http.get = AsyncMock(
        return_value=MagicMock(
            status_code=200,
            json=lambda: {"threat": {"threatTypes": ["SOCIAL_ENGINEERING"]}},
        )
    )
    with (
        patch(
            "app.services.url_expand_service.expand_public",
            new=AsyncMock(return_value=CHAIN),
        ),
        TestClient(
            _app(web_risk_key="k", http=http), raise_server_exceptions=True
        ) as client,
    ):
        resp = client.get("/api/v1/expand", params={"url": "https://bit.ly/x"})
    assert resp.json()["web_risk"] == {
        "checked": True,
        "threats": ["SOCIAL_ENGINEERING"],
    }


def test_a_failed_web_risk_call_is_not_cached_as_absence():
    """A transient Web Risk failure must not hide the safety signal for the
    whole cache TTL — the next caller has to ask again."""
    cache = MetaFetchCache(None)
    cache.set = AsyncMock()
    http = MagicMock()
    http.get = AsyncMock(return_value=MagicMock(status_code=503))
    with (
        patch(
            "app.services.url_expand_service.expand_public",
            new=AsyncMock(return_value=CHAIN),
        ),
        TestClient(
            _app(web_risk_key="k", http=http, cache=cache),
            raise_server_exceptions=True,
        ) as client,
    ):
        resp = client.get("/api/v1/expand", params={"url": "https://bit.ly/x"})
    assert resp.json()["web_risk"] is None
    cache.set.assert_not_called()


def test_a_spent_budget_stops_asking_google():
    """The expander shares one project quota with the safety analyzer but
    is the only public consumer, so it yields first: past its cap the
    verdict is absent and no lookup leaves the box."""
    http = MagicMock()
    http.get = AsyncMock()
    budget = AsyncMock()
    budget.take = AsyncMock(return_value=False)
    with (
        patch(
            "app.services.url_expand_service.expand_public",
            new=AsyncMock(return_value=CHAIN),
        ),
        TestClient(
            _app(web_risk_key="k", http=http, budget=budget),
            raise_server_exceptions=True,
        ) as client,
    ):
        resp = client.get("/api/v1/expand", params={"url": "https://bit.ly/x"})
    assert resp.json()["web_risk"] is None
    http.get.assert_not_awaited()


def test_a_lookup_we_declined_to_make_is_still_cached():
    """Unlike an unanswered ask, a spent budget is a sustained state — not
    caching it would re-enter this path on every single request."""
    cache = MetaFetchCache(None)
    cache.set = AsyncMock()
    budget = AsyncMock()
    budget.take = AsyncMock(return_value=False)
    with (
        patch(
            "app.services.url_expand_service.expand_public",
            new=AsyncMock(return_value=CHAIN),
        ),
        TestClient(
            _app(web_risk_key="k", cache=cache, budget=budget),
            raise_server_exceptions=True,
        ) as client,
    ):
        client.get("/api/v1/expand", params={"url": "https://bit.ly/x"})
    cache.set.assert_awaited_once()
