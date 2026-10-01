"""Google Web Risk client — infrastructure/web_risk.py."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.infrastructure.web_risk import (
    DISPLAY_THREAT_TYPES,
    ENFORCEMENT_THREAT_TYPES,
    WebRiskClient,
)


def _http(payload, status=200):
    http = AsyncMock()
    http.get = AsyncMock(
        return_value=SimpleNamespace(status_code=status, json=lambda: payload)
    )
    return http


@pytest.mark.asyncio
async def test_the_key_never_rides_the_query_string():
    """httpx logs full request URLs at INFO, so a key in the query string
    lands in the log store in plaintext on every lookup."""
    http = _http({})
    await WebRiskClient(
        http, api_key="k123", threat_types=ENFORCEMENT_THREAT_TYPES
    ).lookup("https://ok.example/x")

    args, kwargs = http.get.await_args
    assert kwargs["headers"]["X-Goog-Api-Key"] == "k123"
    # Scoped to the whole call, not just params: the key must not reappear
    # in the URL or anywhere else a request logger would render.
    kwargs_without_headers = {k: v for k, v in kwargs.items() if k != "headers"}
    assert "k123" not in f"{args}{kwargs_without_headers}"


@pytest.mark.asyncio
async def test_matched_threat_types_are_returned():
    http = _http({"threat": {"threatTypes": ["MALWARE"], "expireTime": "x"}})
    assert await WebRiskClient(
        http, api_key="k", threat_types=ENFORCEMENT_THREAT_TYPES
    ).lookup("https://bad.example") == ["MALWARE"]


@pytest.mark.asyncio
async def test_clean_url_returns_an_empty_list():
    client = WebRiskClient(
        _http({}), api_key="k", threat_types=ENFORCEMENT_THREAT_TYPES
    )
    assert await client.lookup("https://ok.example") == []


@pytest.mark.asyncio
async def test_uncategorised_match_still_counts():
    http = _http({"threat": {"expireTime": "x"}})
    assert await WebRiskClient(
        http, api_key="k", threat_types=ENFORCEMENT_THREAT_TYPES
    ).lookup("https://bad.example") == ["UNKNOWN"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 429, 500])
async def test_non_200_abstains(status):
    client = WebRiskClient(
        _http({}, status=status), api_key="k", threat_types=ENFORCEMENT_THREAT_TYPES
    )
    assert await client.lookup("https://ok.example") is None


@pytest.mark.asyncio
async def test_transport_failure_abstains():
    http = AsyncMock()
    http.get = AsyncMock(side_effect=RuntimeError("boom"))
    assert (
        await WebRiskClient(
            http, api_key="k", threat_types=ENFORCEMENT_THREAT_TYPES
        ).lookup("https://ok.example")
        is None
    )


@pytest.mark.asyncio
async def test_threat_types_are_caller_chosen():
    http = _http({})
    await WebRiskClient(http, api_key="k", threat_types=DISPLAY_THREAT_TYPES).lookup(
        "https://ok.example"
    )

    _, kwargs = http.get.await_args
    assert kwargs["params"]["threatTypes"] == [
        "MALWARE",
        "SOCIAL_ENGINEERING",
        "UNWANTED_SOFTWARE",
    ]
