"""
App registry types.

Defines the Pydantic model and enums for app entries loaded from apps.yaml.
Used for YAML validation at startup and typed access throughout the codebase.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from app.schemas.dto.requests.api_key import ApiKeyScope


class AppStatus(str, Enum):
    """App availability status."""

    LIVE = "live"
    COMING_SOON = "coming_soon"


class AppType(str, Enum):
    """App authentication type."""

    DEVICE_AUTH = "device_auth"


class AppEntry(BaseModel):
    """A single app entry from the registry."""

    name: str = Field(min_length=1, max_length=100)
    icon: str | None = None
    description: str = Field(min_length=1, max_length=300)
    verified: bool = False
    status: AppStatus = AppStatus.COMING_SOON
    type: AppType = AppType.DEVICE_AUTH
    redirect_uris: list[str] = []
    links: dict[str, str] = {}
    # Scopes granted to the app at consent time. Required (non-empty) for
    # live device apps — the registry loader skips live entries without them.
    scopes: list[ApiKeyScope] = []
    # Legacy display strings. Consent and API responses now derive copy
    # from `scopes`; kept for back-compat parsing of coming_soon entries.
    permissions: list[str] = []

    def is_live_device_app(self) -> bool:
        """Check if this app can participate in the device auth consent flow."""
        return self.status == AppStatus.LIVE and self.type == AppType.DEVICE_AUTH
