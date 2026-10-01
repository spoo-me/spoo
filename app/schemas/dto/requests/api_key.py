"""
Request DTOs for API key management endpoints.

CreateApiKeyRequest — POST /api/v1/keys
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field, field_validator

from app.schemas.dto.base import RequestBase


class ApiKeyScope(str, Enum):
    """Permission scopes for credentials (API keys and app-scoped tokens)."""

    SHORTEN_CREATE = "shorten:create"
    URLS_MANAGE = "urls:manage"
    URLS_READ = "urls:read"
    STATS_READ = "stats:read"
    DOMAINS_MANAGE = "domains:manage"
    DOMAINS_READ = "domains:read"
    REPORTS_CREATE = "reports:create"
    WEBHOOKS_MANAGE = "webhooks:manage"
    WEBHOOKS_READ = "webhooks:read"
    # Grantable only via app registry entries (device auth). API keys must
    # never carry it — a key that can mint keys defeats revocation.
    KEYS_MANAGE = "keys:manage"
    ADMIN_ALL = "admin:all"


# Scopes an API key may be created with. keys:manage is deliberately
# excluded so an API key can never create/list/delete other API keys
# (admin:all does not imply it either — see KEYS_MANAGE_SCOPES).
ALLOWED_SCOPES = frozenset(ApiKeyScope) - {ApiKeyScope.KEYS_MANAGE}


class CreateApiKeyRequest(RequestBase):
    """Request body for POST /api/v1/keys."""

    name: str = Field(
        min_length=1,
        max_length=255,
        description="Human-readable key name",
        examples=["My Production Key"],
    )
    description: str | None = Field(
        default=None,
        max_length=1000,
        description="Optional description of what this key is used for",
        examples=["Used by the mobile app for URL shortening"],
    )
    scopes: list[str] = Field(
        description="Permission scopes for the key",
        examples=[["shorten:create", "stats:read"]],
    )
    expires_at: str | int | None = Field(
        default=None,
        description="Expiration time. ISO 8601 string (e.g. `2026-01-01T00:00:00Z`) or Unix epoch seconds (e.g. `1735689599`). Omit for non-expiring key.",
        examples=["2026-01-01T00:00:00Z", 1735689599],
    )

    @field_validator("name", mode="after")
    @classmethod
    def _name_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name is required")
        return v

    @field_validator("scopes", mode="after")
    @classmethod
    def _validate_scopes(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("scopes must be a non-empty array")
        invalid = set(v) - ALLOWED_SCOPES
        if invalid:
            raise ValueError(f"invalid scope(s): {', '.join(invalid)}")
        return v
