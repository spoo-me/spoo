"""
Response DTOs for URL shortening and management endpoints.

UrlResponse       — POST /api/v1/shorten  (201)
UpdateUrlResponse — PATCH /api/v1/urls/{url_id}  (200)
UrlListItem       — one element inside UrlListResponse.items
UrlListResponse   — GET /api/v1/urls  (200)

Response shapes are the API contract — field names match the existing Flask
endpoints exactly, including the camelCase keys in UrlListResponse
(``pageSize``, ``hasNext``, ``sortBy``, ``sortOrder``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from bson import ObjectId
from pydantic import Field

from app.schemas.dto.base import ResponseBase
from app.schemas.dto.responses.tag import TagRef
from app.schemas.models.base import ANONYMOUS_OWNER_ID
from app.schemas.models.tag import TagDoc
from app.schemas.models.url import AbVariant, LinkMetaTags, UrlStatus, UrlV2Doc
from app.shared.datetime_utils import to_unix_timestamp


def _tag_refs(doc: UrlV2Doc, tag_refs: dict[ObjectId, TagDoc] | None) -> list[TagRef]:
    """The link's tags in its own order; ids the registry no longer has are skipped."""
    if not doc.tag_ids or not tag_refs:
        return []
    return [TagRef.from_doc(tag_refs[i]) for i in doc.tag_ids if i in tag_refs]


class MetaTagsResponse(ResponseBase):
    """Custom social-preview settings on a URL (client-visible fields only)."""

    title: str = Field(description="og:title.", examples=["We just launched 🎉"])
    description: str | None = Field(default=None, description="og:description.")
    image: str | None = Field(default=None, description="og:image URL.")
    color: str | None = Field(
        default=None, description="Discord embed accent color.", examples=["#FF5733"]
    )
    warnings: list[str] | None = Field(
        default=None,
        description="Non-fatal quality notes, e.g. an image WhatsApp may drop.",
    )

    @classmethod
    def from_model(cls, meta: LinkMetaTags | None) -> MetaTagsResponse | None:
        if meta is None:
            return None
        return cls(
            title=meta.title,
            description=meta.description,
            image=meta.image,
            color=meta.color,
            warnings=meta.image_warnings() or None,
        )


class AbVariantResponse(ResponseBase):
    """One A/B split destination."""

    url: str = Field(description="Variant destination URL.")
    weight: int = Field(description="Percentage of visitors sent here.")

    @classmethod
    def from_model(
        cls, variants: list[AbVariant] | None
    ) -> list[AbVariantResponse] | None:
        if not variants:
            return None
        return [cls(url=v.url, weight=v.weight) for v in variants]


_AB_VARIANTS_RESP_DESC = (
    "Weighted split destinations ({url, weight} entries; the default "
    "destination takes the remaining share), or null."
)


class UrlResponse(ResponseBase):
    """Response body for a newly created shortened URL (POST /api/v1/shorten).

    ``created_at`` is a Unix timestamp integer — matching the existing endpoint.
    """

    id: str = Field(
        description=(
            "MongoDB ObjectId of the URL — the identifier the management "
            "endpoints (`/urls/{url_id}`) address it by."
        ),
        examples=["507f1f77bcf86cd799439011"],
    )
    alias: str = Field(description="Short code for the URL.", examples=["mylink"])
    short_url: str = Field(
        description=(
            "Full shortened URL ready for sharing. Emoji aliases appear "
            "unencoded (clients/browsers percent-encode on use)."
        ),
        examples=["https://spoo.me/mylink", "https://spoo.me/🚀🔥"],
    )
    long_url: str = Field(
        description="Original destination URL.",
        examples=["https://example.com/long/url"],
    )
    owner_id: str | None = Field(
        default=None,
        description="User ID if authenticated, null for anonymous URLs.",
        examples=["507f1f77bcf86cd799439011"],
    )
    created_at: int = Field(
        description="Creation time as Unix timestamp.",
        examples=[1704067200],
    )
    status: UrlStatus = Field(
        description=(
            "URL status. Derived — reflects time and max-click expiry even "
            "before the stored flip is persisted."
        ),
        examples=["ACTIVE"],
    )
    private_stats: bool | None = Field(
        default=None,
        description="Whether statistics are private (owner-only).",
    )
    geo_rules: dict[str, str] | None = Field(
        default=None,
        description="Per-country destination overrides (ISO alpha-2 code → URL), or null.",
        examples=[{"IN": "https://example.in/"}],
    )
    expired_redirect_url: str | None = Field(
        default=None,
        description="Destination served once the link has expired, or null.",
        examples=["https://example.com/offer-ended"],
    )
    tags: list[TagRef] = Field(
        default_factory=list,
        description="The link's tags (id, name, colour), in the link's order.",
    )
    ab_variants: list[AbVariantResponse] | None = Field(
        default=None,
        description=_AB_VARIANTS_RESP_DESC,
        examples=[[{"url": "https://example.com/b", "weight": 40}]],
    )
    meta_tags: MetaTagsResponse | None = Field(
        default=None, description="Custom social preview, if configured."
    )
    claim_token: str | None = Field(
        default=None,
        description=(
            "One-time bearer proof of creation — your deed to this link. "
            "Present only on anonymous creates, shown exactly once (only "
            "its hash is retained server-side). Store it to later attach "
            "the link to an account via POST /api/v1/urls/claim; null for "
            "authenticated creates."
        ),
    )

    @classmethod
    def from_doc(
        cls,
        doc: UrlV2Doc,
        base_url: str,
        *,
        claim_token: str | None = None,
        tag_refs: dict[ObjectId, TagDoc] | None = None,
    ) -> UrlResponse:
        """Build from a UrlV2Doc and the canonical base URL.

        ``base_url`` is the public origin under which the short link will be
        served — ``settings.app_url`` for system-default URLs and
        ``https://<fqdn>`` for custom domains. Built at the route layer.
        ``claim_token`` passes through verbatim — the doc only holds the hash.
        """
        return cls(
            id=str(doc.id),
            alias=doc.alias,
            short_url=f"{base_url.rstrip('/')}/{doc.alias}",
            long_url=doc.long_url,
            owner_id=str(doc.owner_id)
            if doc.owner_id and doc.owner_id != ANONYMOUS_OWNER_ID
            else None,
            created_at=to_unix_timestamp(doc.created_at, default=0),
            status=doc.effective_status,
            private_stats=doc.private_stats,
            geo_rules=doc.geo_rules,
            expired_redirect_url=doc.expired_redirect_url,
            tags=_tag_refs(doc, tag_refs),
            ab_variants=AbVariantResponse.from_model(doc.ab_variants),
            meta_tags=MetaTagsResponse.from_model(doc.meta_tags),
            claim_token=claim_token,
        )


class UpdateUrlResponse(ResponseBase):
    """Response body after a successful URL update (PATCH /api/v1/urls/{url_id})."""

    id: str = Field(
        description="MongoDB ObjectId of the URL.",
        examples=["507f1f77bcf86cd799439011"],
    )
    alias: str | None = Field(
        default=None, description="Short code.", examples=["mylink"]
    )
    long_url: str | None = Field(
        default=None,
        description="Destination URL.",
        examples=["https://example.com/long/url"],
    )
    status: UrlStatus | None = Field(
        default=None,
        description=(
            "URL status. Derived — reflects time and max-click expiry even "
            "before the stored flip is persisted."
        ),
        examples=["ACTIVE"],
    )
    password_set: bool = Field(description="Whether the URL is password-protected.")
    max_clicks: int | None = Field(
        default=None, description="Click limit, or null if unlimited.", examples=[100]
    )
    expire_after: int | None = Field(
        default=None,
        description="Expiration as Unix timestamp, or null.",
        examples=[1735689599],
    )
    starts_at: int | None = Field(
        default=None,
        description="Go-live time as Unix timestamp, or null when live now.",
        examples=[1789500000],
    )
    pre_start_url: str | None = Field(
        default=None,
        description="Where visitors go before `starts_at`, or null for the not-yet-live page.",
        examples=["https://example.com/coming-soon"],
    )
    block_bots: bool | None = Field(
        default=None, description="Whether bot blocking is enabled."
    )
    private_stats: bool | None = Field(
        default=None, description="Whether statistics are private."
    )
    domain: str | None = Field(
        default=None,
        description="Domain fqdn the URL is served on. Null for the system default.",
        examples=["links.acme.com"],
    )
    geo_rules: dict[str, str] | None = Field(
        default=None,
        description="Per-country destination overrides (ISO alpha-2 code → URL), or null.",
        examples=[{"IN": "https://example.in/"}],
    )
    expired_redirect_url: str | None = Field(
        default=None,
        description="Destination served once the link has expired, or null.",
        examples=["https://example.com/offer-ended"],
    )
    tags: list[TagRef] = Field(
        default_factory=list,
        description="The link's tags (id, name, colour), in the link's order.",
    )
    ab_variants: list[AbVariantResponse] | None = Field(
        default=None,
        description=_AB_VARIANTS_RESP_DESC,
        examples=[[{"url": "https://example.com/b", "weight": 40}]],
    )
    updated_at: int = Field(
        description="Last update time as Unix timestamp.", examples=[1704067200]
    )
    meta_tags: MetaTagsResponse | None = Field(
        default=None, description="Custom social preview, if configured."
    )

    @classmethod
    def from_doc(
        cls, doc: UrlV2Doc, tag_refs: dict[ObjectId, TagDoc] | None = None
    ) -> UpdateUrlResponse:
        """Build from a UrlV2Doc after an update operation."""
        return cls(
            id=str(doc.id),
            alias=doc.alias,
            long_url=doc.long_url,
            status=doc.effective_status,
            password_set=doc.password is not None,
            max_clicks=doc.max_clicks,
            expire_after=to_unix_timestamp(doc.expire_after),
            starts_at=to_unix_timestamp(doc.starts_at),
            pre_start_url=doc.pre_start_url,
            block_bots=doc.block_bots,
            private_stats=doc.private_stats,
            domain=doc.domain,
            geo_rules=doc.geo_rules,
            expired_redirect_url=doc.expired_redirect_url,
            tags=_tag_refs(doc, tag_refs),
            ab_variants=AbVariantResponse.from_model(doc.ab_variants),
            updated_at=to_unix_timestamp(doc.updated_at, default=0),
            meta_tags=MetaTagsResponse.from_model(doc.meta_tags),
        )


class UrlListItem(ResponseBase):
    """A single URL entry inside UrlListResponse.items.

    ``created_at`` and ``last_click`` are ISO 8601 strings (e.g. "2024-01-01T00:00:00Z").
    ``expire_after`` is a Unix timestamp integer or null.
    These formats match the existing endpoint exactly.
    ``status`` is derived — it reflects time and max-click expiry even
    before the stored flip is persisted.
    """

    id: str
    alias: str | None = None
    long_url: str | None = None
    status: UrlStatus | None = None
    created_at: datetime | None = None
    expire_after: int | None = None  # Unix timestamp or null
    starts_at: int | None = None  # Unix timestamp or null
    pre_start_url: str | None = None
    max_clicks: int | None = None
    private_stats: bool | None = None
    block_bots: bool | None = None
    password_set: bool
    total_clicks: int | None = None
    last_click: datetime | None = None
    domain: str | None = None
    geo_rules: dict[str, str] | None = None
    expired_redirect_url: str | None = None
    tags: list[TagRef] = Field(default_factory=list)
    ab_variants: list[AbVariantResponse] | None = None
    meta_tags: MetaTagsResponse | None = None

    @classmethod
    def from_doc(
        cls, doc: UrlV2Doc, tag_refs: dict[ObjectId, TagDoc] | None = None
    ) -> UrlListItem:
        """Build from a UrlV2Doc for URL list responses."""

        def _ensure_utc(dt: datetime | None) -> datetime | None:
            if dt is None:
                return None
            if not dt.tzinfo:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)

        return cls(
            id=str(doc.id),
            alias=doc.alias,
            long_url=doc.long_url,
            status=doc.effective_status,
            created_at=_ensure_utc(doc.created_at),
            expire_after=to_unix_timestamp(doc.expire_after),
            starts_at=to_unix_timestamp(doc.starts_at),
            pre_start_url=doc.pre_start_url,
            max_clicks=doc.max_clicks,
            private_stats=doc.private_stats,
            block_bots=bool(doc.block_bots) if doc.block_bots is not None else None,
            password_set=doc.password is not None,
            total_clicks=doc.total_clicks,
            last_click=_ensure_utc(doc.last_click),
            domain=doc.domain,
            geo_rules=doc.geo_rules,
            expired_redirect_url=doc.expired_redirect_url,
            tags=_tag_refs(doc, tag_refs),
            ab_variants=AbVariantResponse.from_model(doc.ab_variants),
            meta_tags=MetaTagsResponse.from_model(doc.meta_tags),
        )


class DeleteUrlResponse(ResponseBase):
    """Response body for DELETE /api/v1/urls/{url_id}."""

    message: str = Field(description="Confirmation message.", examples=["URL deleted"])
    id: str = Field(
        description="ID of the deleted URL.", examples=["507f1f77bcf86cd799439011"]
    )


class BulkDeleteUrlsResponse(ResponseBase):
    """Response body for DELETE /api/v1/urls?domain=<fqdn> (bulk delete)."""

    message: str = Field(
        description="Confirmation message.",
        examples=["deleted 42 URLs on links.acme.com"],
    )
    count: int = Field(description="Number of URLs deleted.", examples=[42])
    domain: str = Field(
        description="Domain whose URLs were deleted.", examples=["links.acme.com"]
    )


class UrlListResponse(ResponseBase):
    """Response body for GET /api/v1/urls.

    Uses camelCase field names to match the existing Flask endpoint exactly.
    Field names are camelCase here (not snake_case + alias) because this is a
    response-only model — we build it explicitly in the route handler.
    """

    items: list[UrlListItem]
    page: int
    pageSize: int
    total: int
    hasNext: bool
    sortBy: str
    sortOrder: str


class AliasCheckResponse(ResponseBase):
    """Response body for GET /api/v1/shorten/check-alias.

    ``available`` is true only when the alias passes format/length validation
    AND is not already taken. When false, ``reason`` explains why so the UI
    can render a precise, non-generic message.
    """

    available: bool = Field(description="Whether the alias is free to use.")
    reason: str | None = Field(
        default=None,
        description=(
            "When unavailable: 'length', 'format', 'reserved', 'taken', or "
            "'emoji_policy' (emoji alias contains sequences outside the "
            "accepted set — ZWJ, flags, keycaps, or too-new emoji)."
        ),
        examples=["taken"],
    )


class ClaimResultItem(ResponseBase):
    """Per-item outcome of a claim batch."""

    url_id: str = Field(description="The url_id from the request item.")
    status: Literal["claimed", "already_yours", "invalid"] = Field(
        description=(
            "`claimed` — ownership transferred and the token burned; "
            "`already_yours` — you already own this URL (idempotent "
            "repeat); `invalid` — unknown id, wrong token, or a link that "
            "is not claimable (deliberately indistinguishable)."
        ),
    )


class ClaimUrlsResponse(ResponseBase):
    """Response body for POST /api/v1/urls/claim.

    The batch never hard-fails: every submitted item gets a result, in
    request order.
    """

    results: list[ClaimResultItem] = Field(
        description="One outcome per submitted item, in request order."
    )
    claimed: int = Field(description="Convenience count of `claimed` results.")
