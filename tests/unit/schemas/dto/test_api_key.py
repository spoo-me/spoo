"""Unit tests for API key request and response DTOs."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.dto.requests.api_key import CreateApiKeyRequest
from app.schemas.dto.responses.api_key import (
    ApiKeyCreatedResponse,
)

# ── CreateApiKeyRequest ────────────────────────────────────────────────────────


class TestCreateApiKeyRequest:
    def test_valid(self):
        req = CreateApiKeyRequest.model_validate(
            {"name": "My Key", "scopes": ["shorten:create"]}
        )
        assert req.name == "My Key"
        assert req.description is None
        assert req.expires_at is None

    @pytest.mark.parametrize(
        "payload",
        [
            {"name": "  ", "scopes": ["shorten:create"]},  # blank name
            {"name": "Key", "scopes": []},  # empty scopes
            {"name": "Key", "scopes": ["unknown:scope"]},  # invalid scope
        ],
        ids=["blank_name", "empty_scopes", "invalid_scope"],
    )
    def test_invalid_input_rejected(self, payload):
        with pytest.raises(ValidationError):
            CreateApiKeyRequest.model_validate(payload)

    def test_valid_all_scopes(self):
        req = CreateApiKeyRequest.model_validate(
            {
                "name": "Admin Key",
                "scopes": [
                    "shorten:create",
                    "urls:manage",
                    "urls:read",
                    "stats:read",
                    "admin:all",
                ],
            }
        )
        assert len(req.scopes) == 5

    def test_optional_fields(self):
        req = CreateApiKeyRequest.model_validate(
            {
                "name": "Key",
                "scopes": ["shorten:create"],
                "description": "test key",
                "expires_at": 9999999999,
            }
        )
        assert req.description == "test key"
        assert req.expires_at == 9999999999


# ── ApiKeyCreatedResponse ─────────────────────────────────────────────────────


class TestApiKeyCreatedResponse:
    def test_has_token_field(self):
        r = ApiKeyCreatedResponse(
            id="507f1f77bcf86cd799439011",
            name="My Key",
            scopes=["shorten:create"],
            created_at=1704067200,
            revoked=False,
            token_prefix="AbCdEfGh",
            token="spoo_AbCdEfGhIjKlMnOpQrStUvWxYz",
        )
        d = r.model_dump()
        assert d["token"] == "spoo_AbCdEfGhIjKlMnOpQrStUvWxYz"
        assert d["token_prefix"] == "AbCdEfGh"

    def test_expires_at_null(self):
        r = ApiKeyCreatedResponse(
            id="507f1f77bcf86cd799439011",
            name="Key",
            scopes=["shorten:create"],
            created_at=1704067200,
            revoked=False,
            token_prefix="AbCdEfGh",
            token="spoo_abc",
        )
        assert r.expires_at is None
