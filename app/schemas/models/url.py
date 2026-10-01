"""
URL document models.

Three separate schemas map to three MongoDB collections:

  UrlV2Doc     → urlsV2     (current schema, ObjectId _id, separate alias field)
  LegacyUrlDoc → urls       (v1 schema, short_code is _id, hyphenated field names,
                              embedded analytics, plaintext password)
  EmojiUrlDoc  → emojis     (same structure as LegacyUrlDoc)
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.models.base import ANONYMOUS_OWNER_ID, MongoBaseModel, PyObjectId
from app.shared.datetime_utils import as_aware_utc
from app.shared.url_utils import (
    link_destination_urls,
    parse_destination,
    secondary_hosts,
    secondary_registrables,
)

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


class UrlStatus(str, Enum):
    """Status values for v2 URLs."""

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    EXPIRED = "EXPIRED"
    BLOCKED = "BLOCKED"
    # Derived only (starts_at in the future); never persisted.
    SCHEDULED = "SCHEDULED"


class SchemaVersion(str, Enum):
    """URL collection / schema version identifiers."""

    V2 = "v2"
    V1 = "v1"
    EMOJI = "emoji"


# og:image rendering cliffs, documented per platform. Above/below these,
# platforms silently degrade or drop the image — we warn, never reject
# (the cliffs are platform-specific; the image may be fine elsewhere).
WHATSAPP_RELIABLE_BYTES = 300_000  # WhatsApp silently drops above ~this
MIN_IMAGE_SIDE_PX = 200  # below: Facebook/WhatsApp may drop entirely
LARGE_CARD_MIN_WIDTH_PX = 600  # below: Facebook demotes to small thumbnail
MAX_ASPECT_RATIO = 4  # above 4:1: WhatsApp drops


class MetaImageMeta(BaseModel):
    """Validation metadata for meta_tags.image — written by the upload
    path (synchronously) or the async image validator, never by clients."""

    width: int | None = None
    height: int | None = None
    bytes: int | None = None
    content_type: str | None = None
    checked_at: datetime


class LinkMetaTags(BaseModel):
    """Custom social-preview tags. Presence on a URL = feature enabled.

    title is mandatory (a card without one renders broken everywhere).
    image is an https URL (R2-hosted or external; data URIs are converted
    to R2 URLs before this model is built); SVG rejected — no preview
    crawler renders it. color = Discord embed border (theme-color).
    """

    title: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=240)
    image: str | None = Field(default=None, max_length=2048)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    image_meta: MetaImageMeta | None = None
    updated_at: datetime | None = None
    updated_ip: str | None = None  # audit trail, mirrors creation_ip

    @field_validator("title", "description", mode="before")
    @classmethod
    def _strip_control(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = _CONTROL_CHARS_RE.sub("", v).strip()
        return v

    @field_validator("image")
    @classmethod
    def _image_https_no_svg(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not v.startswith("https://"):
            raise ValueError("image must be an https:// URL")
        if urlparse(v).path.lower().endswith(".svg"):
            raise ValueError("SVG images are not supported by preview crawlers")
        return v

    def image_warnings(self) -> list[str]:
        """Platform-cliff notes for the og:image, derived from image_meta.

        Empty until image_meta exists — for external https images that's
        after async validation records it; uploads sniff synchronously.
        Unknown dimensions skip the dimension checks rather than guess.
        """
        im = self.image_meta
        if im is None:
            return []
        warnings: list[str] = []
        if im.bytes and im.bytes > WHATSAPP_RELIABLE_BYTES:
            warnings.append("image exceeds 300KB; WhatsApp may silently drop it")
        if im.width and im.height:
            if im.width < MIN_IMAGE_SIDE_PX or im.height < MIN_IMAGE_SIDE_PX:
                warnings.append(
                    "image is smaller than 200x200; Facebook and WhatsApp may "
                    "drop it entirely"
                )
            elif im.width < LARGE_CARD_MIN_WIDTH_PX:
                warnings.append(
                    "image is narrower than 600px; Facebook renders a small "
                    "thumbnail instead of the large card (1200x630 recommended)"
                )
            if max(im.width, im.height) > MAX_ASPECT_RATIO * min(im.width, im.height):
                warnings.append(
                    "image aspect ratio exceeds 4:1; WhatsApp drops extreme ratios"
                )
        return warnings


class UrlDestination(BaseModel):
    """Parsed destination parts, stamped at create/edit time.

    Enables index-driven lookups (takedowns, sweeps, blocklist application)
    over ``dest.registrable_domain`` without regex scans of long_url. Absent
    on docs written before the backfill (``scripts/backfill_url_dest.py``).
    """

    scheme: str = ""
    host: str = ""
    subdomain: str = ""
    registrable_domain: str = ""
    # Hosts the link can ALSO route to (geo rules today, A/B variants
    # later), main host excluded. Enforcement matches host OR these.
    secondary_hosts: list[str] = Field(default_factory=list)
    # Registrable domain of each secondary host, same index. Feed sweeps
    # fan out by domain; this is what lets them reach a hidden host.
    secondary_registrable: list[str] = Field(default_factory=list)

    @classmethod
    def from_url(cls, url: str | None) -> UrlDestination | None:
        parts = parse_destination(url)
        return cls(**parts) if parts else None

    @classmethod
    def for_link(
        cls,
        long_url: str | None,
        *,
        geo_rules: dict[str, str] | None = None,
        variants: list[str] | None = None,
        pre_start_url: str | None = None,
        expired_redirect_url: str | None = None,
    ) -> UrlDestination | None:
        """Main parts plus every secondary host. None only when nothing
        about the link parses."""
        main = parse_destination(long_url)
        urls = link_destination_urls(
            None,
            geo_rules=geo_rules,
            variants=variants,
            pre_start_url=pre_start_url,
            expired_redirect_url=expired_redirect_url,
        )
        exclude = (main or {}).get("host", "")
        extra = secondary_hosts(urls, exclude=exclude)
        if main is None and not extra:
            return None
        return cls(
            **(main or {}),
            secondary_hosts=extra,
            secondary_registrable=secondary_registrables(urls, exclude=exclude),
        )

    def to_doc(self) -> dict:
        """Mongo shape: secondary_hosts absent when empty so the sparse index
        holds only links that actually have them. The backfill is the one
        writer of an empty list, as its convergence marker for geo links
        whose rules add no host (see scripts/backfill_url_dest.py)."""
        data = self.model_dump()
        for field in ("secondary_hosts", "secondary_registrable"):
            if not data[field]:
                data.pop(field)
        return data


class AbVariant(BaseModel):
    """One A/B split destination. ``weight`` is the percentage of visitors
    sent here; long_url receives whatever the variants leave over."""

    url: str
    weight: int = Field(ge=1, le=100)


class UrlV2Doc(MongoBaseModel):
    """Document model for the `urlsV2` collection.

    password stores an argon2 hash. owner_id uses ANONYMOUS_OWNER_ID for
    unowned URLs. domain scopes alias uniqueness via the compound
    `(domain, alias)` index.
    """

    alias: str
    owner_id: PyObjectId = Field(default=ANONYMOUS_OWNER_ID)
    domain: str

    @field_validator("owner_id", mode="before")
    @classmethod
    def _coerce_null_owner(cls, v: Any) -> Any:
        return v if v is not None else ANONYMOUS_OWNER_ID

    @field_validator("domain", mode="before")
    @classmethod
    def _normalise_domain(cls, v: Any) -> str:
        # Reject empty so a forgotten domain on insert can't silently shadow
        # a real short under the unique compound index. Strip first so
        # whitespace-only input is also caught.
        if v is None:
            raise ValueError(
                "domain is required — pass settings.system_default_domain or "
                "an explicit custom domain fqdn"
            )
        normalised = str(v).strip()
        if normalised == "":
            raise ValueError(
                "domain is required — pass settings.system_default_domain or "
                "an explicit custom domain fqdn"
            )
        return normalised.lower().rstrip(".")

    created_at: datetime
    creation_ip: str | None = None
    # First-party client that created the link (X-Spoo-Client slug from
    # FIRST_PARTY_CLIENTS, e.g. "dashboard", "snap"). None for untagged,
    # unknown-client, and legacy creations.
    created_via: str | None = None
    long_url: str
    # Parsed destination parts (see UrlDestination). None/absent when the
    # destination had no parseable hostname or the doc predates the backfill.
    dest: UrlDestination | None = None
    password: str | None = None
    block_bots: bool | None = None
    max_clicks: int | None = Field(default=None, ge=0)
    expire_after: datetime | None = None
    # UTC instant before which the link is not live; None = live now.
    starts_at: datetime | None = None
    # Where pre-start visitors go; None = the not-yet-live page.
    pre_start_url: str | None = None
    # ISO 3166-1 alpha-2 country code (uppercase) → destination URL override.
    # long_url stays the fallback for unmatched countries. None = no targeting.
    geo_rules: dict[str, str] | None = None
    # Ids into the owner's ``tags`` collection. Docs written before the
    # field existed read as [].
    tag_ids: list[PyObjectId] = Field(default_factory=list)
    # Weighted split destinations; weights sum to at most 100 and long_url
    # takes the remainder. Applied only when no geo rule matched.
    ab_variants: list[AbVariant] | None = None
    # Where visitors go once the link is EXPIRED; INACTIVE and BLOCKED never use it.
    expired_redirect_url: str | None = None
    status: UrlStatus = UrlStatus.ACTIVE
    # Enforcement audit trail, stamped when safety flips status to BLOCKED.
    # ``updated_at`` is lossy (any later touch overwrites); these survive.
    # Also the only reason-of-record for per-link blocks, which
    # deliberately write no host-wide verdict.
    blocked_at: datetime | None = None
    blocked_reason: str | None = None
    # Host causes of the block; reactivates only when the last one is removed.
    # None marks a per-link block that no host unblock may undo.
    blocked_hosts: list[str] | None = None
    # Stamped on reversal; blocked_at/blocked_reason stay.
    unblocked_at: datetime | None = None
    private_stats: bool | None = True  # None for anonymous/unowned URLs
    meta_tags: LinkMetaTags | None = None
    total_clicks: int = Field(default=0, ge=0)
    last_click: datetime | None = None
    updated_at: datetime | None = None
    # Claim tokens: sha256 of the one-time bearer token minted on anonymous
    # creates; unset once claimed. Both fields are stripped when None at
    # insert so the owner_claimed partial index can filter on $exists.
    claim_token_hash: str | None = None
    claimed_at: datetime | None = None

    @property
    def effective_status(self) -> UrlStatus:
        """Status with derived expiry folded in — time lapse and max-click
        exhaustion read as EXPIRED even before the persisted flip (the
        flip happens on the click/redirect path; between observations the
        stored field may lag), and a future ``starts_at`` reads as
        SCHEDULED (expiry wins: an expired link is dead for good). Never
        read ``status`` raw on a wire path.
        """
        if self.status is not UrlStatus.ACTIVE:
            return self.status
        expire_after = as_aware_utc(self.expire_after)
        if expire_after is not None and expire_after <= datetime.now(timezone.utc):
            return UrlStatus.EXPIRED
        if self.max_clicks is not None and self.total_clicks >= self.max_clicks:
            return UrlStatus.EXPIRED
        starts_at = as_aware_utc(self.starts_at)
        if starts_at is not None and starts_at > datetime.now(timezone.utc):
            return UrlStatus.SCHEDULED
        return self.status


class LegacyUrlDoc(MongoBaseModel):
    """
    Document model for the `urls` collection (v1 schema).

    Key differences from v2:
    - `_id` IS the short code string (not an ObjectId).
    - Field names use hyphens: `max-clicks`, `total-clicks`, `block-bots`, etc.
        Pydantic field aliases map these to valid Python identifiers.
    - Analytics are embedded directly on the URL document.
    - Password is stored in plaintext.
    - No owner_id, no status field.

    Note: `id` inherited from MongoBaseModel is typed as Optional[PyObjectId],
    but for v1 documents it holds a plain string. We override it here with
    Optional[Any] and rely on from_mongo() to pass through whatever _id value
    MongoDB returns. Repositories never interpret this field as an ObjectId.
    """

    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=True,
    )

    # _id is the short code string for v1 — override base type
    id: Any | None = Field(default=None, alias="_id")

    url: str
    password: str | None = None

    # Safety enforcement. v1 predates the v2 status machine; a single
    # ``blocked`` flag is the whole state space it needs (absent = active,
    # so ten million untouched docs stay valid with zero backfill).
    # Blocking beats the historical manual delete: evidence preserved,
    # reversible, and the clicker gets the 451 instead of a bare 404.
    blocked: bool = False
    blocked_at: datetime | None = None
    blocked_reason: str | None = None
    unblocked_at: datetime | None = None

    @property
    def effective_status(self) -> UrlStatus:
        """v1 has no status machine, so the flag IS the state. A property,
        like the v2 twin: two funnels with different call conventions is
        how a caller gets a TypeError on a live route."""
        return UrlStatus.BLOCKED if self.blocked else UrlStatus.ACTIVE

    # Hyphenated field names — use aliases matching exact MongoDB keys
    max_clicks: int | None = Field(default=None, alias="max-clicks")
    total_clicks: int = Field(default=0, alias="total-clicks")
    block_bots: bool | None = Field(default=None, alias="block-bots")
    expiration_time: datetime | None = Field(default=None, alias="expiration-time")
    last_click: str | None = Field(default=None, alias="last-click")
    last_click_browser: str | None = Field(default=None, alias="last-click-browser")
    last_click_os: str | None = Field(default=None, alias="last-click-os")
    last_click_country: str | None = Field(default=None, alias="last-click-country")

    # Embedded analytics (dynamic dict fields — not typed further to preserve
    # the arbitrary key structure used for country/browser/os/referrer tracking)
    ips: list[str] = Field(default_factory=list)
    counter: dict[str, int] = Field(default_factory=dict)
    unique_counter: dict[str, int] = Field(default_factory=dict)
    country: dict[str, Any] = Field(default_factory=dict)
    browser: dict[str, Any] = Field(default_factory=dict)
    os_name: dict[str, Any] = Field(default_factory=dict)
    referrer: dict[str, Any] = Field(default_factory=dict)
    bots: dict[str, int] = Field(default_factory=dict)
    average_redirection_time: float = 0.0


class EmojiUrlDoc(LegacyUrlDoc):
    """
    Document model for the `emojis` collection.

    Identical structure to LegacyUrlDoc — the only difference is which
    MongoDB collection it lives in. Repositories use the correct collection.
    """
