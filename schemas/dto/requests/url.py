"""
Request DTOs for URL shortening and management endpoints.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

from schemas.dto.base import RequestBase
from schemas.dto.requests._descriptions import LIST_URLS_FILTER_DESC
from schemas.models.url import UrlStatus
from shared.datetime_utils import parse_datetime
from shared.emoji_policy import is_emoji_only_shape
from shared.url_utils import normalise_fqdn

ALLOWED_SORT_FIELDS = frozenset({"created_at", "last_click", "total_clicks"})

_GEO_URL_MAX_LENGTH = 8192  # same bound as long_url

# Hard sanity ceiling, NOT the product cap (that's settings.geo_rules_max_
# countries, service-enforced). This normaliser runs pre-auth on every
# request body — the ceiling bounds the loop for anonymous callers.
_GEO_RULES_HARD_CAP = 500


_ALNUM_ALIAS_RE = re.compile(r"[a-zA-Z0-9_-]+\Z")

_ALIAS_FIELD_DESC = (
    "Custom short code. Either alphanumeric (a-z, A-Z, 0-9, `_`, `-`; "
    "3-16 chars) or emoji-only (1-15 fully-qualified emoji — no ZWJ "
    "sequences, flags, or keycaps). Auto-generated if omitted."
)


def _validate_alias_shape(v: str | None) -> str | None:
    """Structural alias gate shared by create/update.

    Alphanumeric aliases enforce their 3-16 char bounds here (422, exactly
    like the old ``pattern=`` violations). Anything else must at least be
    emoji-shaped — mixed emoji+text and garbage fail fast as 422. The
    emoji *policy* (qualification, version caps, grapheme count) is
    service-enforced (400): the caps are settings-configurable and DTOs
    stay settings-free.
    """
    if v is None:
        return v
    if _ALNUM_ALIAS_RE.fullmatch(v):
        if not (3 <= len(v) <= 16):
            raise ValueError("alphanumeric alias must be 3-16 characters")
        return v
    if not is_emoji_only_shape(v):
        raise ValueError(
            "alias must be alphanumeric (a-z, A-Z, 0-9, _, -) or emoji-only"
        )
    return v


def _normalise_geo_rules(v: dict | None) -> dict | None:
    """Normalise geo_rules keys to uppercase ISO codes.

    JSON parsing guarantees key uniqueness, but normalisation could merge
    keys like `"in"` and `"IN"` — reject that instead of silently picking one.
    Semantic validation (real ISO codes, URL safety) lives in the service layer.
    """
    if v is None:
        return None
    if not isinstance(v, dict):
        # mode="before" runs ahead of type coercion — pass non-dicts through
        # so Pydantic rejects them with a normal 422 instead of us crashing.
        return v
    if len(v) > _GEO_RULES_HARD_CAP:
        raise ValueError(f"geo_rules cannot exceed {_GEO_RULES_HARD_CAP} entries")
    # The product cap is enforced in the service layer from settings
    # (geo_rules_max_countries) — same split as max_emoji_alias_length.
    normalised: dict[str, str] = {}
    for key, url in v.items():
        code = str(key).strip().upper()
        if code in normalised:
            raise ValueError(f"duplicate country code after normalisation: '{code}'")
        if not isinstance(url, str) or not url.strip():
            raise ValueError(f"geo_rules['{code}'] must be a non-empty URL string")
        if len(url) > _GEO_URL_MAX_LENGTH:
            raise ValueError(
                f"geo_rules['{code}'] URL exceeds {_GEO_URL_MAX_LENGTH} characters"
            )
        normalised[code] = url.strip()
    return normalised


class UrlFilter(RequestBase):
    """Parsed structure for the ``filter`` query parameter in ListUrlsQuery."""

    status: UrlStatus | None = None
    created_after: str | int | None = Field(
        default=None,
        alias="createdAfter",
        description="Filter URLs created after this time. ISO 8601 string or Unix epoch seconds.",
    )
    created_before: str | int | None = Field(
        default=None,
        alias="createdBefore",
        description="Filter URLs created before this time. ISO 8601 string or Unix epoch seconds.",
    )
    password_set: bool | None = Field(default=None, alias="passwordSet")
    max_clicks_set: bool | None = Field(default=None, alias="maxClicksSet")
    search: str | None = Field(default=None, max_length=500)


class MetaTagsRequest(BaseModel):
    """Custom social preview (og:title / og:description / og:image / theme-color)."""

    title: str = Field(
        min_length=1,
        max_length=120,
        description="Preview headline (og:title). Required when meta_tags is set.",
        examples=["We just launched 🎉"],
    )
    description: str | None = Field(
        default=None,
        max_length=240,
        description="og:description — roughly 200 chars render on most platforms.",
    )
    image: str | None = Field(
        default=None,
        # Coarse body guard; the real decoded cap is R2_UPLOAD_MAX_BYTES in
        # ingest_meta_image. ~512KB x 4/3 base64 — raise both to lift the cap.
        max_length=700_000,
        description=(
            "og:image — an https URL, or a `data:image/png|jpeg|webp;base64,` "
            "URI which is validated and stored on spoo's CDN. 1200x630 "
            "recommended; keep it under 300KB or WhatsApp silently drops it; "
            "SVG is rejected (no preview crawler renders it)."
        ),
        examples=["https://example.com/og.png"],
    )
    color: str | None = Field(
        default=None,
        pattern=r"^#[0-9a-fA-F]{6}$",
        description="Accent color shown on Discord embeds (theme-color).",
        examples=["#FF5733"],
    )

    @field_validator("image")
    @classmethod
    def _image_scheme(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if v.startswith("data:image/"):
            return v  # decoded, sniffed, and size-capped by the ingest step
        if not v.startswith("https://"):
            raise ValueError("image must be an https:// URL or an image data URI")
        if len(v) > 2048:
            raise ValueError("image URL must be at most 2048 characters")
        return v


_META_TAGS_FIELD_DESC = (
    "Custom social preview served to link-preview crawlers (WhatsApp, Discord, "
    "Slack, iMessage, …). The object replaces the whole setting; on PATCH pass "
    "null to remove. Requires a verified account with the feature enabled. "
    "Note: platforms cache previews for ~7-30 days — edits propagate slowly "
    "(the Facebook Sharing Debugger, LinkedIn Post Inspector, and Telegram's "
    "@WebpageBot force a refresh)."
)


class CreateUrlRequest(RequestBase):
    """Request body for creating a new shortened URL.

    Accepts ``url`` as an alias for ``long_url`` — the existing API supports both.
    """

    long_url: str = Field(
        max_length=8192,
        validation_alias=AliasChoices("long_url", "url"),
        description="The destination URL to shorten. Must be a valid http:// or https:// URL.",
        examples=["https://example.com/very/long/url/path"],
    )
    alias: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description=_ALIAS_FIELD_DESC,
        examples=["mylink", "🚀🔥"],
    )
    alias_type: Literal["alphanumeric", "emoji"] = Field(
        default="alphanumeric",
        description=(
            "Alias style to auto-generate when `alias` is omitted. "
            "Ignored when `alias` is provided."
        ),
    )
    password: str | None = Field(
        default=None,
        min_length=8,
        max_length=128,
        description="Password to protect the URL. Min 8 chars, must contain letter + number + special char.",
        examples=["secure@123"],
    )
    block_bots: bool | None = Field(
        default=None,
        description="Block known bot user agents from accessing the URL.",
    )
    max_clicks: int | None = Field(
        default=None,
        gt=0,
        description="Maximum clicks before the URL expires. Must be positive.",
        examples=[100],
    )
    expire_after: datetime | None = Field(
        default=None,
        description="Expiration time. ISO 8601 string (e.g. `2025-12-31T23:59:59Z`) or Unix epoch seconds (e.g. `1735689599`).",
        examples=["2025-12-31T23:59:59Z", 1735689599],
    )
    private_stats: bool | None = Field(
        default=None,
        description="Make statistics private (only owner can view). Requires authentication.",
    )
    domain: str | None = Field(
        default=None,
        max_length=253,
        description=(
            "Custom domain fqdn to scope the short link under (e.g. "
            "`links.acme.com`). Requires authentication and ownership of an "
            "ACTIVE custom domain. Omit for the default spoo.me namespace."
        ),
        examples=["links.acme.com"],
    )
    geo_rules: dict[str, str] | None = Field(
        default=None,
        description=(
            "Per-country destination overrides: ISO 3166-1 alpha-2 country "
            "code → destination URL (at most 50 entries by default). Visitors from a listed "
            "country are redirected to that URL; everyone else gets the "
            "default destination (`url`). Requires authentication."
        ),
        examples=[{"IN": "https://example.in/", "US": "https://example.com/us"}],
    )
    meta_tags: MetaTagsRequest | None = Field(
        default=None, description=_META_TAGS_FIELD_DESC
    )

    @field_validator("alias")
    @classmethod
    def _alias_shape(cls, v: str | None) -> str | None:
        return _validate_alias_shape(v)

    @field_validator(
        "expire_after", mode="before", json_schema_input_type=datetime | int | None
    )
    @classmethod
    def _parse_expire_after(cls, v: str | int | None) -> datetime | None:
        if v is None:
            return None
        result = parse_datetime(v)
        if result is None:
            raise ValueError("Invalid expire_after format")
        return result

    @field_validator("domain", mode="before")
    @classmethod
    def _norm_domain(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        return normalise_fqdn(v)

    @field_validator("geo_rules", mode="before")
    @classmethod
    def _norm_geo_rules(cls, v: dict | None) -> dict | None:
        return _normalise_geo_rules(v)


class UpdateUrlRequest(RequestBase):
    """Request body for partially updating an existing shortened URL.

    All fields are optional; only provided fields are updated.
    Pass ``max_clicks=0`` or ``max_clicks=null`` to remove the limit.
    Pass ``password=null`` (or omit) to remove password protection.
    """

    long_url: str | None = Field(
        default=None,
        max_length=8192,
        validation_alias=AliasChoices("long_url", "url"),
        description="New destination URL. Must be a valid http:// or https:// URL.",
        examples=["https://example.com/updated/url"],
    )
    alias: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description=(
            "New custom short code (alphanumeric 3-16 chars, or emoji-only "
            "1-15 emoji). Pass `null` to keep existing. Must be unique and "
            "available."
        ),
        examples=["newlink", "🚀🔥"],
    )
    password: str | None = Field(
        default=None,
        min_length=8,
        max_length=128,
        description="New password. Pass `null` to remove password protection.",
        examples=["newPass@456"],
    )
    block_bots: bool | None = Field(
        default=None,
        description="Block known bot user agents. Pass `null` to keep existing setting.",
    )
    # 0 is allowed here to remove the limit (service layer interprets 0 as "remove")
    max_clicks: int | None = Field(
        default=None,
        ge=0,
        description="New click limit. Pass `0` or `null` to remove the limit.",
        examples=[500],
    )
    expire_after: datetime | None = Field(
        default=None,
        description="Expiration time. ISO 8601 string (e.g. `2025-12-31T23:59:59Z`) or Unix epoch seconds (e.g. `1735689599`). Pass `null` to remove.",
        examples=["2025-12-31T23:59:59Z", 1735689599],
    )
    private_stats: bool | None = Field(
        default=None,
        description="Make statistics private (only owner can view). Pass `null` to keep existing.",
    )
    status: Literal[UrlStatus.ACTIVE, UrlStatus.INACTIVE] | None = Field(
        default=None,
        description="URL status. ACTIVE enables redirects, INACTIVE disables them.",
        examples=["ACTIVE"],
    )
    domain: str | None = Field(
        default=None,
        max_length=253,
        description=(
            "Move the URL to a different domain namespace. Pass an owned ACTIVE "
            "custom-domain fqdn (e.g. `links.acme.com`) to move it there, or "
            "`null`/empty to move it back to the system default. Alias must be "
            "available on the target domain."
        ),
        examples=["links.acme.com"],
    )
    geo_rules: dict[str, str] | None = Field(
        default=None,
        description=(
            "Per-country destination overrides: ISO 3166-1 alpha-2 country "
            "code → destination URL (at most 50 entries by default). The map replaces any "
            "existing rules in full. Pass `null` or `{}` to remove all rules; "
            "omit to keep existing rules unchanged."
        ),
        examples=[{"IN": "https://example.in/", "US": "https://example.com/us"}],
    )
    meta_tags: MetaTagsRequest | None = Field(
        default=None, description=_META_TAGS_FIELD_DESC
    )

    @field_validator("alias")
    @classmethod
    def _alias_shape(cls, v: str | None) -> str | None:
        return _validate_alias_shape(v)

    @field_validator(
        "expire_after", mode="before", json_schema_input_type=datetime | int | None
    )
    @classmethod
    def _parse_expire_after(cls, v: str | int | None) -> datetime | None:
        if v is None:
            return None
        result = parse_datetime(v)
        if result is None:
            raise ValueError("Invalid expire_after format")
        return result

    @field_validator("domain", mode="before")
    @classmethod
    def _norm_domain(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        return normalise_fqdn(v)

    @field_validator("geo_rules", mode="before")
    @classmethod
    def _norm_geo_rules(cls, v: dict | None) -> dict | None:
        return _normalise_geo_rules(v)


class UpdateUrlStatusRequest(BaseModel):
    """Request body for updating only the status of a shortened URL."""

    status: Literal[UrlStatus.ACTIVE, UrlStatus.INACTIVE] = Field(
        description="New status for the URL. `ACTIVE` enables redirects, `INACTIVE` disables them.",
        examples=["ACTIVE"],
    )


class ListUrlsQuery(RequestBase):
    """Query parameters for listing a user's URLs with pagination and filtering.

    The ``filter`` / ``filterBy`` parameter accepts a JSON-encoded ``UrlFilter``
    object.  Call ``get_parsed_filter()`` to obtain the typed sub-model; invalid
    JSON raises ``ValueError`` which FastAPI converts to a 422 response.
    """

    page: int = Field(
        default=1,
        ge=1,
        description="Page number (default: 1)",
        examples=[1],
    )
    page_size: int = Field(
        default=20,
        ge=1,
        le=100,
        alias="pageSize",
        description="Items per page (default: 20, max: 100)",
        examples=[20],
    )
    sort_by: Literal["created_at", "last_click", "total_clicks"] = Field(
        default="created_at",
        alias="sortBy",
        description="Field to sort by",
    )
    sort_order: Literal["ascending", "asc", "1", "descending", "desc", "-1"] = Field(
        default="descending",
        alias="sortOrder",
        description="Sort direction",
    )
    # Raw JSON string; also accepted as ``filterBy`` (the existing API supports both)
    filter: str | None = Field(
        default=None,
        max_length=10000,
        description=LIST_URLS_FILTER_DESC,
        examples=[
            '{"status":"ACTIVE"}',
            '{"passwordSet": true}',
            '{"createdAfter": "2024-01-01T00:00:00Z"}',
            '{"status": "ACTIVE", "maxClicksSet": true}',
            '{"search": "example"}',
            '{"createdAfter": "2024-01-01", "createdBefore": "2024-12-31", "status": "ACTIVE"}',
        ],
    )
    filter_by: str | None = Field(
        default=None,
        max_length=10000,
        alias="filterBy",
        description="Alias for filter parameter.",
    )
    domain: str | None = Field(
        default=None,
        max_length=253,
        description="Filter URLs by exact custom domain fqdn.",
        examples=["links.acme.com"],
    )
    # Parsed result — populated by the model validator, invisible to FastAPI/OpenAPI
    _parsed_filter: UrlFilter | None = PrivateAttr(default=None)

    @field_validator("domain", mode="before")
    @classmethod
    def _norm_domain(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        return normalise_fqdn(v)

    @model_validator(mode="after")
    def _parse_filter_json(self) -> ListUrlsQuery:
        raw = self.filter or self.filter_by
        if not raw:
            return self
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("filter must be valid JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("filter must be a JSON object")
        self._parsed_filter = UrlFilter.model_validate(data)
        return self

    @property
    def parsed_filter(self) -> UrlFilter | None:
        return self._parsed_filter


class AliasCheckQuery(RequestBase):
    """Query parameters for GET /api/v1/shorten/check-alias.

    Intentionally permissive on ``max_length`` so that out-of-range input returns
    a structured ``{available: false, reason: "length"}`` response instead of a
    422 — the UI surfaces the reason inline without re-implementing rules.
    """

    alias: str = Field(
        min_length=1,
        max_length=64,
        description="Candidate alias to check (alphanumeric or emoji-only).",
        examples=["mylink", "🚀🔥"],
    )
    domain: str | None = Field(
        default=None,
        max_length=253,
        description=(
            "Scope the availability check to a custom domain fqdn. Requires "
            "authentication + ownership of an ACTIVE custom domain. Omit for "
            "the system default namespace."
        ),
        examples=["links.acme.com"],
    )

    @field_validator("domain", mode="before")
    @classmethod
    def _norm_domain(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        return normalise_fqdn(v)


class ClaimItemRequest(RequestBase):
    """One (url_id, token) pair in a claim batch."""

    url_id: str = Field(
        description="ObjectId of the URL to claim (from the shorten response).",
        min_length=24,
        max_length=24,
        pattern=r"^[0-9a-f]{24}$",
        examples=["507f1f77bcf86cd799439011"],
    )
    token: str = Field(
        description=(
            "The one-time claim_token returned by the anonymous shorten "
            "call — the bearer proof of creation."
        ),
        # Issued tokens are 43 chars (32 urlsafe bytes); anything shorter
        # is garbage worth rejecting before it costs a lookup.
        min_length=43,
        max_length=256,
    )


class ClaimUrlsRequest(RequestBase):
    """Request body for POST /api/v1/urls/claim (bulk-first, like reports)."""

    claims: list[ClaimItemRequest] = Field(
        description="URLs to claim. Each item resolves independently.",
        min_length=1,
        max_length=16,
    )
