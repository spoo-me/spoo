"""
Integration tests for legacy stats and export routes.

Covers: GET/POST /stats, GET/POST /stats/<code>,
        GET/POST /export/<code>/<format>.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.dependencies import get_db
from app.routes.legacy.stats import router as legacy_stats_router
from tests.conftest import build_test_app


def _mock_db():
    return MagicMock()


def _mock_legacy_doc(password=None):
    """Return a mock LegacyUrlDoc-like object."""
    doc = MagicMock()
    doc.password = password
    doc.url = "https://example.com"
    return doc


SAMPLE_URL_DATA = {
    "_id": "abc123",
    "url": "https://example.com",
    "password": None,
    "total-clicks": 42,
    "max-clicks": None,
    "expiration-time": None,
    "counter": {"2026-03-10": 5, "2026-03-11": 10},
    "unique_counter": {"2026-03-10": 3, "2026-03-11": 7},
    "browser": {"Chrome": 20, "Firefox": 12},
    "os_name": {"Windows": 18, "macOS": 14},
    "country": {"US": 25, "UK": 10},
    "referrer": {"google.com": 15, "twitter.com": 8},
    "bots": {"Googlebot": 3},
    "unique_browser": {"Chrome": 10, "Firefox": 5},
    "unique_os_name": {"Windows": 9, "macOS": 6},
    "unique_country": {"US": 12, "UK": 5},
    "unique_referrer": {"google.com": 8, "twitter.com": 4},
    "creation-date": "2026-03-10",
    "creation-time": "12:00:00",
    "last-click": "2026-03-11 15:30:00",
    "last-click-browser": "Chrome",
    "last-click-os": "Windows",
    "last-click-country": "US",
    "block-bots": False,
    "average_redirection_time": 15.5,
    "total_unique_clicks": 15,
}


# ── GET /stats tests ─────────────────────────────────────────────────────────


def test_stats_get_renders_form():
    db = _mock_db()
    app = build_test_app(legacy_stats_router, overrides={get_db: lambda: db})
    with TestClient(app) as client:
        resp = client.get("/stats")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


# ── POST /stats tests ────────────────────────────────────────────────────────


def test_stats_post_missing_short_code_returns_400():
    db = _mock_db()
    app = build_test_app(legacy_stats_router, overrides={get_db: lambda: db})
    with TestClient(app) as client:
        resp = client.post("/stats", data={})
    assert resp.status_code == 400


def test_stats_post_url_not_found_renders_form():
    db = _mock_db()

    with patch("app.routes.legacy.stats.LegacyUrlRepository") as MockLegacyRepo:
        MockLegacyRepo.return_value.find_by_id = AsyncMock(return_value=None)

        app = build_test_app(legacy_stats_router, overrides={get_db: lambda: db})
        with TestClient(app) as client:
            resp = client.post("/stats", data={"short_code": "noexist"})
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_stats_post_password_required_renders_prompt():
    db = _mock_db()
    doc = _mock_legacy_doc(password="secret123")

    with patch("app.routes.legacy.stats.LegacyUrlRepository") as MockLegacyRepo:
        MockLegacyRepo.return_value.find_by_id = AsyncMock(return_value=doc)

        app = build_test_app(legacy_stats_router, overrides={get_db: lambda: db})
        with TestClient(app) as client:
            resp = client.post("/stats", data={"short_code": "abc123"})
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_stats_post_wrong_password_renders_error():
    db = _mock_db()
    doc = _mock_legacy_doc(password="secret123")

    with patch("app.routes.legacy.stats.LegacyUrlRepository") as MockLegacyRepo:
        MockLegacyRepo.return_value.find_by_id = AsyncMock(return_value=doc)

        app = build_test_app(legacy_stats_router, overrides={get_db: lambda: db})
        with TestClient(app) as client:
            resp = client.post(
                "/stats", data={"short_code": "abc123", "password": "wrong"}
            )
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_stats_post_correct_password_redirects():
    db = _mock_db()
    doc = _mock_legacy_doc(password="secret123")

    with patch("app.routes.legacy.stats.LegacyUrlRepository") as MockLegacyRepo:
        MockLegacyRepo.return_value.find_by_id = AsyncMock(return_value=doc)

        app = build_test_app(legacy_stats_router, overrides={get_db: lambda: db})
        with TestClient(app, follow_redirects=False) as client:
            resp = client.post(
                "/stats", data={"short_code": "abc123", "password": "secret123"}
            )
    assert resp.status_code == 302
    assert "password=secret123" in resp.headers["location"]


def test_stats_post_no_password_redirects():
    db = _mock_db()
    doc = _mock_legacy_doc(password=None)

    with patch("app.routes.legacy.stats.LegacyUrlRepository") as MockLegacyRepo:
        MockLegacyRepo.return_value.find_by_id = AsyncMock(return_value=doc)

        app = build_test_app(legacy_stats_router, overrides={get_db: lambda: db})
        with TestClient(app, follow_redirects=False) as client:
            resp = client.post("/stats", data={"short_code": "abc123"})
    assert resp.status_code == 302
    assert resp.headers["location"] == "/stats/abc123"


# ── GET/POST /stats/<code> tests ─────────────────────────────────────────────


def test_analytics_get_not_found_returns_404_html():
    db = _mock_db()
    cursor = MagicMock()
    cursor.to_list = AsyncMock(return_value=[])

    with patch("app.routes.legacy.stats.LegacyUrlRepository") as MockLegacyRepo:
        MockLegacyRepo.return_value.aggregate = AsyncMock(return_value=None)

        app = build_test_app(legacy_stats_router, overrides={get_db: lambda: db})
        with TestClient(app) as client:
            resp = client.get("/stats/noexist")
    assert resp.status_code == 404
    assert "text/html" in resp.headers["content-type"]


def test_analytics_post_not_found_returns_404_json():
    db = _mock_db()

    with patch("app.routes.legacy.stats.LegacyUrlRepository") as MockLegacyRepo:
        MockLegacyRepo.return_value.aggregate = AsyncMock(return_value=None)

        app = build_test_app(legacy_stats_router, overrides={get_db: lambda: db})
        with TestClient(app) as client:
            resp = client.post("/stats/noexist")
    assert resp.status_code == 404
    assert "UrlError" in resp.json()


def test_analytics_post_wrong_password_returns_400():
    db = _mock_db()
    url_data = {**SAMPLE_URL_DATA, "password": "secret123"}

    with patch("app.routes.legacy.stats.LegacyUrlRepository") as MockLegacyRepo:
        MockLegacyRepo.return_value.aggregate = AsyncMock(return_value=url_data)

        app = build_test_app(legacy_stats_router, overrides={get_db: lambda: db})
        with TestClient(app) as client:
            resp = client.post("/stats/abc123", data={"password": "wrong"})
    assert resp.status_code == 400
    assert "PasswordError" in resp.json()


def test_analytics_post_returns_json():
    db = _mock_db()

    with patch("app.routes.legacy.stats.LegacyUrlRepository") as MockLegacyRepo:
        MockLegacyRepo.return_value.aggregate = AsyncMock(
            return_value=dict(SAMPLE_URL_DATA)
        )

        app = build_test_app(legacy_stats_router, overrides={get_db: lambda: db})
        with TestClient(app) as client:
            resp = client.post("/stats/abc123")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total-clicks"] == 42
    assert "average_daily_clicks" in data
    assert "average_weekly_clicks" in data
    assert "average_monthly_clicks" in data


def test_analytics_post_missing_avg_redirection_time_stays_numeric_zero():
    """Legacy wire compat: the shipped Flask-era JSON always carried a
    number here and old clients do not null-guard, so the 0 default stays
    on this surface (the modern API uses null for "no measurement")."""
    db = _mock_db()
    url_data = dict(SAMPLE_URL_DATA)
    del url_data["average_redirection_time"]

    with patch("app.routes.legacy.stats.LegacyUrlRepository") as MockLegacyRepo:
        MockLegacyRepo.return_value.aggregate = AsyncMock(return_value=url_data)

        app = build_test_app(legacy_stats_router, overrides={get_db: lambda: db})
        with TestClient(app) as client:
            resp = client.post("/stats/abc123")
    assert resp.status_code == 200
    assert resp.json()["average_redirection_time"] == 0


def test_analytics_get_renders_stats_page():
    db = _mock_db()

    with patch("app.routes.legacy.stats.LegacyUrlRepository") as MockLegacyRepo:
        MockLegacyRepo.return_value.aggregate = AsyncMock(
            return_value=dict(SAMPLE_URL_DATA)
        )

        app = build_test_app(legacy_stats_router, overrides={get_db: lambda: db})
        with TestClient(app) as client:
            resp = client.get("/stats/abc123")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


# ── Export tests ─────────────────────────────────────────────────────────────


def test_export_invalid_format_get_returns_400_html():
    db = _mock_db()
    app = build_test_app(legacy_stats_router, overrides={get_db: lambda: db})
    with TestClient(app) as client:
        resp = client.get("/export/abc123/pdf")
    assert resp.status_code == 400
    assert "text/html" in resp.headers["content-type"]


def test_export_invalid_format_post_returns_400_json():
    db = _mock_db()
    app = build_test_app(legacy_stats_router, overrides={get_db: lambda: db})
    with TestClient(app) as client:
        resp = client.post("/export/abc123/pdf")
    assert resp.status_code == 400
    assert "FormatError" in resp.json()


def test_export_not_found_post_returns_404_json():
    db = _mock_db()

    with patch("app.routes.legacy.stats.LegacyUrlRepository") as MockLegacyRepo:
        MockLegacyRepo.return_value.aggregate = AsyncMock(return_value=None)

        app = build_test_app(legacy_stats_router, overrides={get_db: lambda: db})
        with TestClient(app) as client:
            resp = client.post("/export/noexist/json")
    assert resp.status_code == 404
    assert "UrlError" in resp.json()


def test_export_wrong_password_post_returns_400():
    db = _mock_db()
    url_data = {**SAMPLE_URL_DATA, "password": "secret123"}

    with patch("app.routes.legacy.stats.LegacyUrlRepository") as MockLegacyRepo:
        MockLegacyRepo.return_value.aggregate = AsyncMock(return_value=url_data)

        app = build_test_app(legacy_stats_router, overrides={get_db: lambda: db})
        with TestClient(app) as client:
            resp = client.post("/export/abc123/json", data={"password": "wrong"})
    assert resp.status_code == 400
    assert "PasswordError" in resp.json()


def test_export_json_format():
    db = _mock_db()

    with patch("app.routes.legacy.stats.LegacyUrlRepository") as MockLegacyRepo:
        MockLegacyRepo.return_value.aggregate = AsyncMock(
            return_value=dict(SAMPLE_URL_DATA)
        )

        app = build_test_app(legacy_stats_router, overrides={get_db: lambda: db})
        with TestClient(app) as client:
            resp = client.post("/export/abc123/json")
    assert resp.status_code == 200
    assert "application/json" in resp.headers["content-type"]
    assert "Content-Disposition" in resp.headers


def test_export_csv_format():
    db = _mock_db()

    with patch("app.routes.legacy.stats.LegacyUrlRepository") as MockLegacyRepo:
        MockLegacyRepo.return_value.aggregate = AsyncMock(
            return_value=dict(SAMPLE_URL_DATA)
        )

        app = build_test_app(legacy_stats_router, overrides={get_db: lambda: db})
        with TestClient(app) as client:
            resp = client.post("/export/abc123/csv")
    assert resp.status_code == 200
    assert "application/zip" in resp.headers["content-type"]
    assert "Content-Disposition" in resp.headers


def test_export_xlsx_format():
    db = _mock_db()

    with patch("app.routes.legacy.stats.LegacyUrlRepository") as MockLegacyRepo:
        MockLegacyRepo.return_value.aggregate = AsyncMock(
            return_value=dict(SAMPLE_URL_DATA)
        )

        app = build_test_app(legacy_stats_router, overrides={get_db: lambda: db})
        with TestClient(app) as client:
            resp = client.post("/export/abc123/xlsx")
    assert resp.status_code == 200
    assert "spreadsheetml" in resp.headers["content-type"]
    assert "Content-Disposition" in resp.headers


def test_export_xml_format():
    db = _mock_db()

    with patch("app.routes.legacy.stats.LegacyUrlRepository") as MockLegacyRepo:
        MockLegacyRepo.return_value.aggregate = AsyncMock(
            return_value=dict(SAMPLE_URL_DATA)
        )

        app = build_test_app(legacy_stats_router, overrides={get_db: lambda: db})
        with TestClient(app) as client:
            resp = client.post("/export/abc123/xml")
    assert resp.status_code == 200
    assert "application/xml" in resp.headers["content-type"]
    assert "Content-Disposition" in resp.headers
