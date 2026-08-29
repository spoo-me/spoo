"""Smoke tests: OpenAPI schema generation and route coverage."""

from __future__ import annotations

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/")

from fastapi.testclient import TestClient


def test_openapi_json_returns_valid_json(smoke_client: TestClient) -> None:
    """GET /openapi.json should return parseable JSON."""
    resp = smoke_client.get("/openapi.json")
    assert resp.status_code == 200
    data = resp.json()
    assert "openapi" in data
    assert "paths" in data


def test_openapi_info_section(smoke_client: TestClient) -> None:
    """Info section should contain the correct app title."""
    data = smoke_client.get("/openapi.json").json()
    assert data["info"]["title"] == "spoo.me"
    assert data["info"]["version"] == "1.0.0"


def test_openapi_has_expected_paths(smoke_client: TestClient) -> None:
    """OpenAPI spec should list all critical endpoint paths."""
    data = smoke_client.get("/openapi.json").json()
    paths = set(data["paths"].keys())

    # Routes with include_in_schema=False (legacy/page routes, redirect
    # shortcuts, password form) are intentionally excluded from OpenAPI.
    expected = [
        "/health",
        "/auth/login",
        "/auth/register",
        "/auth/me",
        "/auth/refresh",
        "/auth/logout",
        "/auth/set-password",
        "/auth/send-verification",
        "/auth/verify-email",
        "/auth/request-password-reset",
        "/auth/reset-password",
        "/api/v1/shorten",
        "/api/v1/urls",
        "/api/v1/stats",
        "/api/v1/export",
        "/api/v1/keys",
        "/api/v1/keys/{key_id}",
        "/api/v1/urls/{url_id}",
        "/api/v1/urls/{url_id}/status",
        "/api/v1/urls/bulk/delete",
        "/api/v1/urls/bulk/status",
        "/api/v1/urls/bulk/expiry",
        "/api/v1/urls/bulk/domain",
        "/oauth/providers",
        "/oauth/providers/{provider_name}/unlink",
        "/oauth/{provider}",
        "/oauth/{provider}/callback",
        "/oauth/{provider}/link",
    ]
    for path in expected:
        assert path in paths, f"Missing path: {path}"


def test_openapi_health_has_response_schema(smoke_client: TestClient) -> None:
    """GET /health should document its response body, including the 503 case."""
    data = smoke_client.get("/openapi.json").json()
    responses = data["paths"]["/health"]["get"]["responses"]
    for status in ("200", "503"):
        schema = responses[status]["content"]["application/json"]["schema"]
        assert schema["$ref"] == "#/components/schemas/HealthResponse"


def test_openapi_has_components_schemas(smoke_client: TestClient) -> None:
    """OpenAPI spec should have response schemas defined in components."""
    data = smoke_client.get("/openapi.json").json()
    schemas = data.get("components", {}).get("schemas", {})
    assert len(schemas) > 0, "No schemas found in components"
