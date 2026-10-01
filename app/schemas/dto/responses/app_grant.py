"""
Response DTOs for GET /api/v1/apps.

AppGrantResponse      — one connected app (active device-auth grant)
AppGrantsListResponse — GET /api/v1/apps (200)

The wire serves the spoo-landing Apps page (lib/api/apps.ts). Grants carry
the effective scope slugs plus their derived consent sentences; legacy
grants (pre-scopes) surface an empty scope list and the full-access
sentence.
"""

from __future__ import annotations

from pydantic import Field

from app.schemas.dto.base import ResponseBase, UtcDatetime
from app.schemas.models.app import AppEntry
from app.schemas.models.app_grant import AppGrantDoc
from app.shared.scopes import LEGACY_FULL_ACCESS_DESCRIPTION, describe_scopes


class AppGrantResponse(ResponseBase):
    """A single connected app as returned by the list endpoint."""

    id: str = Field(
        description="Grant ID (accepted as `grant_id` by POST /auth/device/revoke)",
        examples=["665f1c2ab7e94d0c8a1f2b3c"],
    )
    app: str = Field(
        description=(
            "App registry key (config/apps.yaml). Shares a namespace with the "
            "frontend catalogue slugs and is the `app_id` handle for "
            "POST /auth/device/revoke."
        ),
        examples=["spoo-cli"],
    )
    app_name: str = Field(
        description="Display name from the registry (falls back to `app`)",
        examples=["Spoo CLI"],
    )
    icon: str | None = Field(
        default=None,
        description="Registry icon filename, or null when the entry is gone",
        examples=["spoo-cli.svg"],
    )
    scopes: list[str] = Field(
        description=(
            "Effective scope slugs the grant confers. Empty list means a "
            "legacy unrestricted grant (consented before scoped grants "
            "existed) — full account access, not zero access."
        ),
        examples=[["shorten:create", "urls:read"]],
    )
    permissions: list[str] = Field(
        description=(
            "Human-readable consent sentences derived from `scopes` "
            "(full-access sentence for legacy unrestricted grants)"
        ),
        examples=[["Create short links", "List and read links"]],
    )
    granted_at: UtcDatetime = Field(
        description="When access was granted (ISO 8601 UTC)"
    )
    last_used_at: UtcDatetime | None = Field(
        default=None,
        description="Last token exchange/refresh by this app, null if never used",
    )

    @classmethod
    def from_grant(
        cls,
        doc: AppGrantDoc,
        entry: AppEntry | None,
        effective_scopes: list[str] | None,
    ) -> AppGrantResponse:
        # A grant can outlive its registry entry; it still holds live tokens,
        # so it is listed with fallback labels rather than hidden. Legacy
        # grants (effective_scopes None) must never read as "no access":
        # they are full-account until the user re-consents.
        return cls(
            id=str(doc.id),
            app=doc.app_id,
            app_name=entry.name if entry else doc.app_id,
            icon=entry.icon if entry else None,
            scopes=[] if effective_scopes is None else effective_scopes,
            permissions=(
                describe_scopes(effective_scopes)
                if effective_scopes is not None
                else [LEGACY_FULL_ACCESS_DESCRIPTION]
            ),
            granted_at=doc.granted_at,
            last_used_at=doc.last_used_at,
        )


class AppGrantsListResponse(ResponseBase):
    """Response body for GET /api/v1/apps."""

    items: list[AppGrantResponse] = Field(
        description="Active grants, newest granted_at first"
    )
