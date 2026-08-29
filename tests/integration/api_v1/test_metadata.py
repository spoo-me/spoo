"""Tests for GET /api/v1/metadata (destination tag parser endpoint)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from dependencies import get_current_user
from infrastructure.cache.meta_fetch_cache import MetaFetchCache
from infrastructure.safe_fetch import FetchedBody, FetchHardError, FetchTransientError

from .conftest import _build_test_app, _make_user

HTML = b"""<html><head>
<meta property="og:title" content="Dest Title">
<meta property="og:image" content="/og.png">
</head><body></body></html>"""


def _app(user=None):
    app = _build_test_app({get_current_user: lambda: user})
    app.state.meta_fetch_cache = MetaFetchCache(None)  # no-op cache
    return app


def test_metadata_works_anonymously():
    # Auth is optional — the public preview checker calls this logged out.
    body = FetchedBody(HTML, "text/html", "https://dest.example/final")
    with (
        patch("routes.api_v1.metadata.fetch_public", new=AsyncMock(return_value=body)),
        TestClient(_app(user=None), raise_server_exceptions=True) as client,
    ):
        resp = client.get("/api/v1/metadata", params={"url": "https://dest.example/a"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "Dest Title"


def test_metadata_parses_destination():
    body = FetchedBody(HTML, "text/html", "https://dest.example/final")
    with (
        patch("routes.api_v1.metadata.fetch_public", new=AsyncMock(return_value=body)),
        TestClient(_app(user=_make_user()), raise_server_exceptions=True) as client,
    ):
        resp = client.get("/api/v1/metadata", params={"url": "https://dest.example/a"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Dest Title"
    assert data["image"] == "https://dest.example/og.png"  # resolved vs final_url
    assert data["final_url"] == "https://dest.example/final"
    assert data["og"]["title"] == "Dest Title"


def test_metadata_carries_audit_fields():
    page = b"""<html><head>
    <title>HTML Title</title>
    <meta name="description" content="HTML Desc">
    <meta property="og:title" content="OG Title">
    <link rel="icon" href="/fav.png" sizes="32x32">
    </head><body></body></html>"""
    body = FetchedBody(page, "text/html", "https://dest.example/final")
    with (
        patch("routes.api_v1.metadata.fetch_public", new=AsyncMock(return_value=body)),
        TestClient(_app(), raise_server_exceptions=True) as client,
    ):
        resp = client.get("/api/v1/metadata", params={"url": "https://dest.example/a"})
    data = resp.json()
    assert data["html_title"] == "HTML Title"
    assert data["html_description"] == "HTML Desc"
    assert data["favicon"] == "https://dest.example/fav.png"


def test_metadata_rejects_http_url():
    with TestClient(_app(), raise_server_exceptions=False) as client:
        resp = client.get("/api/v1/metadata", params={"url": "http://dest.example"})
    assert resp.status_code == 400


def test_metadata_unfetchable_is_422():
    with (
        patch(
            "routes.api_v1.metadata.fetch_public",
            new=AsyncMock(
                side_effect=FetchHardError("resolves to a non-public address")
            ),
        ),
        TestClient(_app(), raise_server_exceptions=False) as client,
    ):
        resp = client.get(
            "/api/v1/metadata", params={"url": "https://internal.example"}
        )
    assert resp.status_code == 422
    assert resp.json()["code"] == "unfetchable"


def test_metadata_timeout_is_504():
    with (
        patch(
            "routes.api_v1.metadata.fetch_public",
            new=AsyncMock(side_effect=FetchTransientError("timeout")),
        ),
        TestClient(_app(), raise_server_exceptions=False) as client,
    ):
        resp = client.get("/api/v1/metadata", params={"url": "https://slow.example"})
    assert resp.status_code == 504
