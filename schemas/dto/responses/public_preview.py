"""
Response DTOs for GET /api/v1/public/preview/{short_code}.

The wire shape is frozen against the spoo-landing preview page
(lib/api/public-preview.ts). DTOs serialize only — all derivation
(status, withholding, destination splitting) lives in
services/public_preview_service.py.
"""

from __future__ import annotations

from typing import Literal

from schemas.dto.base import ResponseBase


class PreviewDestination(ResponseBase):
    """A destination URL split into display parts."""

    url: str
    domain: str
    path: str
    is_https: bool


class PreviewGeoDestination(PreviewDestination):
    """One geo-rule destination group.

    ``countries`` are ISO 3166-1 alpha-2 codes, sorted ascending — every
    rule is listed, nothing summarized (anti-cloaking transparency).
    """

    countries: list[str]


class PreviewVariantDestination(PreviewDestination):
    """One A/B variant destination with its traffic share in percent."""

    weight: int


class PublicPreviewResponse(ResponseBase):
    """Response body for the public link preview endpoint.

    ``destination``, ``geo_destinations`` and ``variant_destinations`` are
    non-null only while the link is active and not password-protected — the
    preview never reveals a destination the redirect would refuse to serve.
    The rule runs both ways: ``expired_destination`` names where an expired
    link still sends every visitor when its owner set a fallback, so the
    preview never shows LESS than the redirect serves either.
    """

    generation: Literal["v1", "v2"]
    alias: str
    short_url: str
    whitelabel: bool = False
    status: Literal["active", "inactive", "expired", "blocked", "scheduled"]
    created_at: str | None
    # Unix seconds; present only while ``status`` is ``"scheduled"``.
    starts_at: int | None = None
    password_protected: bool
    destination: PreviewDestination | None
    geo_destinations: list[PreviewGeoDestination] | None
    variant_destinations: list[PreviewVariantDestination] | None = None
    # Set only while ``status`` is ``"expired"`` and the owner set a fallback.
    expired_destination: PreviewDestination | None = None
