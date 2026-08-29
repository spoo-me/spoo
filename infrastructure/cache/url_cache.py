"""URL-specific Redis cache.

Stores UrlCacheData as JSON (not pickle) so cache entries are debuggable and
safe to deserialise across Python versions. Keys are scoped by fqdn:
``url_cache:<domain>:<alias>``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import redis.asyncio as aioredis
from pydantic import BaseModel, ConfigDict, Field

from infrastructure.crypto import verify_password as verify_password_hash
from infrastructure.logging import get_logger
from shared.datetime_utils import to_unix_timestamp

if TYPE_CHECKING:
    from schemas.models.url import UrlV2Doc

log = get_logger(__name__)


class UrlCacheData(BaseModel):
    """Unified cache schema covering both v1 (legacy) and v2 URLs."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(alias="_id")  # accepts "_id" from cached JSON, stored as "id"
    alias: str
    long_url: str
    block_bots: bool
    password_hash: str | None
    # Unix timestamp; None = no expiry OR tz-ambiguous v1 value (never expires)
    expiration_time: int | None
    max_clicks: int | None
    url_status: str  # ACTIVE, INACTIVE, BLOCKED, EXPIRED
    schema_version: str  # "v1" or "v2"
    owner_id: str | None  # ObjectId as string; None for v1 URLs
    total_clicks: int = 0  # Live click count for v1 max-clicks check
    domain: str = ""
    # ISO alpha-2 country code → destination URL. None for v1/legacy and
    # non-geo links; entries cached before this field existed deserialize
    # to None (default), so no cache version bump is needed.
    geo_rules: dict[str, str] | None = None
    # Custom meta-tags (v2 only; None = feature disabled on this link).
    # meta_title is the enabled-signal: LinkMetaTags.title is mandatory.
    meta_title: str | None = None
    meta_description: str | None = None
    meta_image: str | None = None
    meta_color: str | None = None
    meta_image_width: int | None = None  # from async image validation
    meta_image_height: int | None = None

    @classmethod
    def from_v2_doc(cls, doc: UrlV2Doc) -> UrlCacheData:
        """Project a UrlV2Doc into the hot-path cache shape.

        On the model (not a service helper) because three callers need it —
        resolve, the edge write-through, the image validator — and a shared
        derived projection belongs with the data, not behind a private
        cross-module import.
        """
        im = doc.meta_tags.image_meta if doc.meta_tags else None
        return cls(
            id=str(doc.id),
            alias=doc.alias,
            long_url=doc.long_url,
            block_bots=bool(doc.block_bots),
            password_hash=doc.password,
            # to_unix_timestamp treats naive as UTC — v2 datetimes come back
            # from Mongo naive-UTC, so this stays host-TZ-independent.
            expiration_time=to_unix_timestamp(doc.expire_after),
            max_clicks=doc.max_clicks,
            url_status=doc.status,
            schema_version="v2",
            owner_id=str(doc.owner_id) if doc.owner_id else None,
            domain=doc.domain,
            geo_rules=doc.geo_rules,
            meta_title=doc.meta_tags.title if doc.meta_tags else None,
            meta_description=doc.meta_tags.description if doc.meta_tags else None,
            meta_image=doc.meta_tags.image if doc.meta_tags else None,
            meta_color=doc.meta_tags.color if doc.meta_tags else None,
            meta_image_width=im.width if im else None,
            meta_image_height=im.height if im else None,
        )

    def is_time_expired(self, now_ts: float) -> bool:
        """Time-lapse check for the hot path. ``expiration_time`` is None
        for links without expiry AND for v1 rows whose stored value is
        tz-ambiguous (see ``_legacy_doc_to_cache``) — both never expire here.
        """
        return self.expiration_time is not None and self.expiration_time <= now_ts

    def verify_password(self, password: str | None) -> bool:
        """Check a password against this URL's stored hash.

        Handles schema-specific hashing: argon2 for v2, plaintext for v1/emoji.
        Returns True if no password is set or if the password matches.
        """
        if not self.password_hash:
            return True
        if password is None:
            return False
        if self.schema_version == "v2":
            return verify_password_hash(password, self.password_hash)
        return password == self.password_hash


class UrlCache:
    def __init__(
        self, redis_client: aioredis.Redis | None, ttl_seconds: int = 300
    ) -> None:
        self._redis = redis_client
        self.ttl_seconds = ttl_seconds

    def _key(self, short_code: str, domain: str) -> str:
        return f"url_cache:{domain}:{short_code}"

    async def get(self, short_code: str, domain: str) -> UrlCacheData | None:
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(self._key(short_code, domain))
            if raw is None:
                return None
            return UrlCacheData.model_validate_json(raw)
        except Exception as e:
            log.warning("url_cache_get_error", short_code=short_code, error=str(e))
            return None

    async def set(self, short_code: str, data: UrlCacheData) -> None:
        # Domain is read from `data.domain` — caller always has the doc and
        # the doc's domain is the canonical source. No redundant parameter.
        if self._redis is None:
            return
        try:
            await self._redis.setex(
                self._key(short_code, data.domain),
                self.ttl_seconds,
                data.model_dump_json(by_alias=True),
            )
        except Exception as e:
            log.error("url_cache_set_error", short_code=short_code, error=str(e))

    async def invalidate(self, short_code: str, domain: str) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.delete(self._key(short_code, domain))
            log.info(
                "cache_invalidated", short_code=short_code, reason="manual_invalidation"
            )
        except Exception as e:
            log.error("url_cache_invalidate_error", short_code=short_code, error=str(e))

    async def invalidate_many(self, short_codes: list[str], domain: str) -> None:
        """Bulk-invalidate cache entries for a list of aliases on one domain.
        Chunked: one unbounded DELETE with six figures of keys would block
        the redirect hot path's Redis."""
        if not short_codes or self._redis is None:
            return
        keys = [self._key(c, domain) for c in short_codes]
        chunk_size = 10_000
        try:
            for i in range(0, len(keys), chunk_size):
                await self._redis.delete(*keys[i : i + chunk_size])
            log.info(
                "cache_invalidated_bulk",
                count=len(short_codes),
                domain=domain,
                reason="bulk_invalidation",
            )
        except Exception as e:
            log.error("url_cache_invalidate_many_error", domain=domain, error=str(e))
