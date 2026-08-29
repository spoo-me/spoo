"""
URL resolution, creation, update, deletion, and listing service.

Extracts business logic from:
  - blueprints/redirector.py  (resolve + dispatch heuristic)
  - builders/create.py        (create)
  - builders/update.py        (update)
  - builders/query.py         (list_by_owner)

Dispatch heuristic (get_url_by_length_and_type) is preserved exactly:
  emoji alias  → emojis collection, schema "emoji"
  7 chars      → urlsV2 first, urls fallback
  6 chars      → urls first, urlsV2 fallback
  other        → urlsV2 first, urls fallback
"""

from __future__ import annotations

import asyncio
import hmac
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

import pycountry
from bson import ObjectId

from errors import (
    AppError,
    BlockedUrlError,
    ConflictError,
    ForbiddenError,
    GoneError,
    NotFoundError,
    ValidationError,
)
from infrastructure.cache.url_cache import UrlCache, UrlCacheData
from infrastructure.crypto import hash_password, hash_token
from infrastructure.logging import get_logger, should_sample
from repositories.blocked_url_repository import BlockedUrlRepository
from repositories.legacy.emoji_url_repository import EmojiUrlRepository
from repositories.legacy.legacy_url_repository import LegacyUrlRepository
from repositories.url_repository import UrlRepository
from schemas.dto.requests.url import (
    ClaimItemRequest,
    CreateUrlRequest,
    ListUrlsQuery,
    MetaTagsRequest,
    UpdateUrlRequest,
)
from services.edge_cache.contract import cache_key
from services.edge_cache.og_writethrough import OgEdgeWritethrough
from services.events.contract import DomainEvent
from services.events.protocol import DomainEventSink
from services.events.sinks import NullDomainEventSink
from services.meta_tags.events import MetaImageValidateEvent
from services.meta_tags.images import ingest_meta_image
from services.safety.policy import UrlPolicyService
from services.webhooks.payloads import (
    build_link_expired,
    event_changes,
    link_owner_id,
    link_snapshot,
)
from shared.generators import generate_secure_token

if TYPE_CHECKING:
    from infrastructure.cloudflare_kv import CloudflareKVClient
    from infrastructure.storage.r2 import R2StorageClient
    from services.meta_tags.sinks import MetaImageValidationSink
from schemas.models.base import ANONYMOUS_OWNER_ID
from schemas.models.url import (
    EmojiUrlDoc,
    LegacyUrlDoc,
    LinkMetaTags,
    SchemaVersion,
    UrlDestination,
    UrlStatus,
    UrlV2Doc,
)
from shared.alias_dispatch import (
    emoji_lookup_candidates,
    resolution_order,
    v2_lookup_code,
)
from shared.datetime_utils import parse_datetime, to_unix_timestamp
from shared.emoji_policy import (
    canonicalize_emoji_alias,
    check_emoji_alias,
    is_emoji_candidate,
    is_emoji_only_shape,
)
from shared.generators import generate_emoji_alias_v2, generate_short_code_v2
from shared.reserved_aliases import is_reserved_alias
from shared.url_utils import extract_hostname
from shared.validators import (
    validate_alias,
    validate_blocked_url,
)

log = get_logger(__name__)

AliasCheckResult = Literal[
    "available", "length", "format", "reserved", "taken", "emoji_policy"
]


def _validate_geo_rules_shape(
    rules: dict[str, str],
    *,
    max_countries: int,
    enabled: bool = True,
) -> None:
    """Entry cap + real ISO codes. Destination URL safety is NOT here — geo
    targets go through the same ``url_policy.check`` gate as long_url.

    Raises:
        ValidationError: with field paths like ``geo_rules.IN``.
    """
    if not enabled:
        raise ValidationError("geo targeting is not available yet", field="geo_rules")
    if len(rules) > max_countries:
        raise ValidationError(
            f"geo_rules cannot exceed {max_countries} country entries",
            field="geo_rules",
        )
    for code in rules:
        if pycountry.countries.get(alpha_2=code) is None:
            raise ValidationError(
                f"'{code}' is not a valid ISO 3166-1 alpha-2 country code",
                field=f"geo_rules.{code}",
            )


# ── Field update handlers ────────────────────────────────────────────────────
#
# Each handler inspects one field on the update request and, if changed,
# writes the new value into `ops`.  Handlers are registered in
# FIELD_HANDLERS and iterated by UrlService.update().
#
# Signature: (request, existing, ops, service) -> None
#   request  — UpdateUrlRequest from the caller
#   existing — current UrlV2Doc from the database
#   ops      — dict collecting $set fields (mutated in place)
#   service  — the UrlService instance (for cross-cutting helpers)


async def _handle_long_url(
    request: UpdateUrlRequest, existing: UrlV2Doc, ops: dict, service: UrlService
) -> None:
    if request.long_url is not None and request.long_url != existing.long_url:
        # Same gate as create — an edit must not be a side door for
        # destinations that would be rejected at creation.
        rejection = await service._url_policy.check(request.long_url)
        if rejection is not None:
            log.info("url_update_rejected", reason=rejection.code)
            raise ValidationError(rejection.public_message, field="long_url")
        ops["long_url"] = request.long_url
        # Destination changed -> re-stamp the parsed parts (meta_tags
        # precedent: plain model_dump dicts go into ops).
        dest = UrlDestination.from_url(request.long_url)
        ops["dest"] = dest.model_dump() if dest else None


async def _handle_alias(
    request: UpdateUrlRequest, existing: UrlV2Doc, ops: dict, service: UrlService
) -> None:
    if request.alias is None:
        return
    # Same validation + canonicalization as create — an edit must not be a
    # side door for aliases that would be rejected at creation. Compare the
    # CANONICAL form against the stored alias so echoing a VS16 variant of
    # the current alias is a no-op, not a self-collision.
    new_alias = service._validate_and_canonicalize_custom_alias(request.alias)
    if new_alias != existing.alias:
        # Scope the collision check to wherever the URL will land. If the same
        # request is also moving the URL to a different domain, we must verify
        # the new alias is free on the *target* tenant — not the current one.
        scope = (
            (request.domain or service._system_default_domain)
            if "domain" in request.model_fields_set
            else existing.domain
        )
        if scope == service._system_default_domain and is_reserved_alias(new_alias):
            log.info("url_alias_reserved", short_code=new_alias, domain=scope)
            raise ValidationError("Alias is reserved", field="alias")
        if not await service.check_alias_available(new_alias, domain=scope):
            log.info(
                "url_alias_conflict",
                short_code=new_alias,
                domain=scope,
            )
            raise ConflictError("Alias is already in use")
        ops["alias"] = new_alias


async def _handle_domain(
    request: UpdateUrlRequest, existing: UrlV2Doc, ops: dict, service: UrlService
) -> None:
    """Move a URL to a different domain namespace.

    Route layer is responsible for verifying the caller owns the target as an
    ACTIVE custom domain (or that it's the system default). Service treats the
    value as opaque.
    """
    if "domain" not in request.model_fields_set:
        return
    target = request.domain or service._system_default_domain
    if target == existing.domain:
        return
    # A domain-only move must not smuggle a reserved alias onto the default
    # domain — reserved names are legal on custom domains, so the alias may
    # be arriving from a namespace where it was fine. When alias is also
    # changing, _handle_alias already vetted the new one against `target`.
    effective_alias = ops.get("alias", existing.alias)
    if target == service._system_default_domain and is_reserved_alias(effective_alias):
        log.info(
            "url_domain_move_alias_reserved",
            short_code=effective_alias,
            from_domain=existing.domain,
        )
        raise ValidationError(
            f"Alias '{effective_alias}' is reserved on {target}", field="domain"
        )
    # If alias isn't also changing, verify the existing alias is free on the
    # target tenant. The alias handler already ran (it's listed first in
    # FIELD_HANDLERS) and validated its own collision against `target`, so we
    # only need to check when alias is staying put.
    if "alias" not in ops and not await service.check_alias_available(
        existing.alias, domain=target
    ):
        log.info(
            "url_domain_move_alias_conflict",
            short_code=existing.alias,
            from_domain=existing.domain,
            to_domain=target,
        )
        raise ConflictError(f"Alias '{existing.alias}' is already in use on {target}")
    ops["domain"] = target


async def _handle_password(
    request: UpdateUrlRequest, existing: UrlV2Doc, ops: dict, service: UrlService
) -> None:
    if "password" not in request.model_fields_set:
        return
    if not request.password and existing.password:
        # Empty string clears the password
        ops["password"] = None
    elif request.password:
        # Always re-hash — argon2 uses random salts so string comparison
        # cannot detect "same password", and the write is cheap.
        ops["password"] = hash_password(request.password)


async def _handle_max_clicks(
    request: UpdateUrlRequest, existing: UrlV2Doc, ops: dict, service: UrlService
) -> None:
    if "max_clicks" not in request.model_fields_set:
        return
    if (request.max_clicks is None or request.max_clicks == 0) and existing.max_clicks:
        ops["max_clicks"] = None
    elif request.max_clicks and request.max_clicks != existing.max_clicks:
        ops["max_clicks"] = request.max_clicks


async def _handle_expire_after(
    request: UpdateUrlRequest, existing: UrlV2Doc, ops: dict, service: UrlService
) -> None:
    if "expire_after" not in request.model_fields_set:
        return
    if request.expire_after is None and existing.expire_after:
        ops["expire_after"] = None
    elif request.expire_after is not None:
        if request.expire_after <= datetime.now(timezone.utc):
            raise ValidationError(
                "expire_after must be in the future", field="expire_after"
            )
        if request.expire_after != existing.expire_after:
            ops["expire_after"] = request.expire_after


async def _handle_status(
    request: UpdateUrlRequest, existing: UrlV2Doc, ops: dict, service: UrlService
) -> None:
    if request.status is not None and request.status != existing.status:
        ops["status"] = request.status


async def _handle_geo_rules(
    request: UpdateUrlRequest, existing: UrlV2Doc, ops: dict, service: UrlService
) -> None:
    if "geo_rules" not in request.model_fields_set:
        return
    if not request.geo_rules:
        # null or {} removes all rules
        if existing.geo_rules:
            ops["geo_rules"] = None
        return
    if request.geo_rules == existing.geo_rules:
        # Changed-check BEFORE validation, like every other handler — a
        # read-modify-write PATCH echoing unchanged rules must not 400
        # because a destination entered the blocklist since creation.
        return
    _validate_geo_rules_shape(
        request.geo_rules,
        max_countries=service._geo_rules_max_countries,
        enabled=service._geo_rules_enabled,
    )
    for geo_code, geo_url in request.geo_rules.items():
        rejection = await service._url_policy.check(geo_url)
        if rejection is not None:
            raise ValidationError(
                rejection.public_message, field=f"geo_rules.{geo_code}"
            )
    ops["geo_rules"] = request.geo_rules


async def _handle_meta_tags(
    request: UpdateUrlRequest, existing: UrlV2Doc, ops: dict, service: UrlService
) -> None:
    if "meta_tags" not in request.model_fields_set:
        return
    if request.meta_tags is None:
        if existing.meta_tags:
            ops["meta_tags"] = None
        return
    # Resolve data-URI uploads first, then validate. Registered after
    # long_url so a same-request destination change is validated against
    # the NEW destination.
    meta_req, image_meta = await service.resolve_meta_image(
        request.meta_tags, existing.owner_id
    )
    await service.validate_meta_tags(
        meta_req, long_url=ops.get("long_url", existing.long_url)
    )
    ops["meta_tags"] = LinkMetaTags(
        title=meta_req.title,
        description=meta_req.description,
        image=meta_req.image,
        color=meta_req.color,
        image_meta=image_meta,
        updated_at=datetime.now(timezone.utc),
    ).model_dump()


def _simple_field_handler(field_name: str) -> Callable:
    """Factory for nullable fields that just need a changed-check."""

    async def handler(
        request: UpdateUrlRequest, existing: UrlV2Doc, ops: dict, service: UrlService
    ) -> None:
        if field_name not in request.model_fields_set:
            return
        value = getattr(request, field_name)
        current = getattr(existing, field_name)
        if value is None and current:
            ops[field_name] = None
        elif value != current:
            ops[field_name] = value

    return handler


FIELD_HANDLERS: dict[str, Callable[..., Awaitable[None]]] = {
    "long_url": _handle_long_url,
    "alias": _handle_alias,
    # `domain` must follow `alias` — the alias handler peeks at the incoming
    # domain to scope its collision check, and the domain handler peeks at
    # `ops` to decide whether the alias still needs verifying on the target.
    "domain": _handle_domain,
    "password": _handle_password,
    "max_clicks": _handle_max_clicks,
    "expire_after": _handle_expire_after,
    "block_bots": _simple_field_handler("block_bots"),
    "private_stats": _simple_field_handler("private_stats"),
    "status": _handle_status,
    "geo_rules": _handle_geo_rules,
    # Must follow long_url — the handler validates against ops["long_url"]
    # when the destination changes in the same request.
    "meta_tags": _handle_meta_tags,
}


# Per-account ceiling on claimed-in links, enforced at claim time: refusing
# loudly beats the read side silently undercounting stats past its backstop
# cap. ~10x any organic wizard user; becomes the free-tier quota number once
# plan quotas exist.
CLAIM_LIMIT_PER_ACCOUNT = 500


@dataclass(frozen=True)
class ClaimResult:
    """Per-item outcome of a claim batch (never a batch-level failure)."""

    url_id: str
    status: Literal["claimed", "already_yours", "invalid"]


class UrlService:
    def __init__(
        self,
        url_repo: UrlRepository,
        legacy_repo: LegacyUrlRepository,
        emoji_repo: EmojiUrlRepository,
        blocked_url_repo: BlockedUrlRepository,
        url_cache: UrlCache,
        blocked_self_domains: list[str],
        system_default_domain: str,
        url_policy: UrlPolicyService,
        blocked_url_regex_timeout: float = 0.2,
        max_emoji_alias_length: int = 15,
        emoji_accept_max_version: float = 15.1,
        emoji_generate_max_version: float = 12.0,
        emoji_generated_alias_length: int = 3,
        geo_rules_max_countries: int = 50,
        geo_rules_enabled: bool = False,
        og_writethrough: OgEdgeWritethrough | None = None,
        edge_kv: CloudflareKVClient | None = None,
        r2_storage: R2StorageClient | None = None,
        meta_image_max_bytes: int = 512_000,
        meta_image_sink: MetaImageValidationSink | None = None,
        meta_key_secret: str = "",
        events: DomainEventSink | None = None,
    ) -> None:
        self._url_repo = url_repo
        self._legacy_repo = legacy_repo
        self._emoji_repo = emoji_repo
        self._blocked_url_repo = blocked_url_repo
        self._url_cache = url_cache
        self._blocked_self_domains = blocked_self_domains
        # The L0 gate — sole authority on whether a destination may be
        # written (create AND edit). Shares provider instances with the
        # safety analyzer via the wiring.
        self._url_policy = url_policy
        # The only domain on which v1/legacy lookups fire — custom domains
        # are v2-only by definition.
        self._system_default_domain = system_default_domain
        self._blocked_url_regex_timeout = blocked_url_regex_timeout
        self._max_emoji_alias_length = max_emoji_alias_length
        self._emoji_accept_max_version = emoji_accept_max_version
        self._emoji_generate_max_version = emoji_generate_max_version
        self._emoji_generated_alias_length = emoji_generated_alias_length
        self._geo_rules_max_countries = geo_rules_max_countries
        self._geo_rules_enabled = geo_rules_enabled
        # Edge KV write-through for og-links; None when edge cache is
        # unconfigured (self-host) — origin then serves all previews.
        self._og_writethrough = og_writethrough
        # Direct KV handle for purging plain links' hot-promoted entries
        # on takedown-shaped mutations (delete/deactivate/rename/move) —
        # same instance the write-through wraps; None when edge is off.
        self._edge_kv = edge_kv
        self._edge_purge_tasks: set[asyncio.Task] = set()
        # R2 upload target for data-URI og:images; None ⇒ uploads rejected,
        # https image URLs unaffected (self-host degradation).
        self._r2_storage = r2_storage
        self._meta_image_max_bytes = meta_image_max_bytes
        # Async validation for EXTERNAL https images (uploads are validated
        # synchronously). None ⇒ validation skipped (no queue Redis).
        self._meta_image_sink = meta_image_sink
        # HMAC pepper for storage-key owner prefixes (public URLs must not
        # carry raw ObjectIds). Wired from settings.secret_key.
        self._meta_key_secret = meta_key_secret
        # Domain-event sink (webhooks backbone). Null default: producers
        # never carry conditionals and tests need no wiring changes.
        self._events = events or NullDomainEventSink()

    # ── Public API ────────────────────────────────────────────────────────────

    async def resolve(
        self, short_code: str, *, domain: str | None = None
    ) -> tuple[UrlCacheData, SchemaVersion]:
        """
        Resolve a short code to UrlCacheData and schema version.

        ``domain`` scopes the lookup to a custom tenant. None or the system
        default falls back to the original cross-collection path (v2 + v1
        + emoji). Custom tenants only resolve against urlsV2 — v1/emoji
        predate per-domain scoping and never live on custom hostnames.

        Returns (UrlCacheData, schema_version) where schema_version is
        a SchemaVersion enum member (V2, V1, or EMOJI).

        Raises:
            NotFoundError:   URL not found in any collection.
            BlockedUrlError: URL status is BLOCKED (v2 only).
            GoneError:       URL status is EXPIRED or INACTIVE (v2 only),
                             or the expiration time has passed (any schema).
        """
        scope = domain or self._system_default_domain
        is_custom = scope != self._system_default_domain
        # Emoji codes can arrive as byte-variant forms (stray VS16 from
        # keyboards / copy-paste). Canonicalize once here so the cache key
        # and every v2 lookup use one form; the raw form is kept for exact
        # legacy ``emojis`` ``_id`` matches inside _dispatch.
        raw_code = short_code
        short_code = v2_lookup_code(short_code)
        # 1. Cache hit
        cached = await self._url_cache.get(short_code, scope)
        if cached is not None:
            schema = cached.schema_version
            # BLOCKED raises on every schema (v1/emoji carry the safety
            # flag); EXPIRED/INACTIVE only exist on v2.
            if cached.url_status == UrlStatus.BLOCKED or (
                schema == SchemaVersion.V2
                and cached.url_status in (UrlStatus.EXPIRED, UrlStatus.INACTIVE)
            ):
                log.info(
                    "url_resolve_non_active",
                    short_code=short_code,
                    status=cached.url_status,
                    schema=schema,
                    source="cache",
                )
                _raise_for_status(cached.url_status)
            await self._raise_if_time_expired(cached, schema, short_code, "cache")
            if should_sample("cache_operation"):
                log.debug(
                    "url_cache_hit",
                    short_code=short_code,
                    schema=schema,
                    status=cached.url_status,
                )
            return cached, schema

        # 2. Cache miss — dispatch by length and type
        if should_sample("cache_operation"):
            log.debug("url_cache_miss", short_code=short_code)
        if is_custom:
            url_cache_data, schema = await self._dispatch_custom_domain(
                short_code, scope
            )
        else:
            url_cache_data, schema = await self._dispatch(short_code, raw_code)
        if url_cache_data is None:
            log.info("url_resolve_not_found", short_code=short_code, domain=scope)
            raise NotFoundError("URL not found")

        # 3. Populate cache according to caching rules
        await self._populate_cache(short_code, url_cache_data, schema)

        # 4a. Raise for non-ACTIVE (after caching minimal data). BLOCKED
        # applies to every schema; EXPIRED/INACTIVE are v2-only states.
        if url_cache_data.url_status == UrlStatus.BLOCKED or (
            schema == SchemaVersion.V2
            and url_cache_data.url_status in (UrlStatus.EXPIRED, UrlStatus.INACTIVE)
        ):
            log.info(
                "url_resolve_non_active",
                short_code=short_code,
                status=url_cache_data.url_status,
                schema=schema,
                source="db",
            )
            _raise_for_status(url_cache_data.url_status)

        # 4b. Raise for v1 URLs whose max-clicks have been exhausted
        if (
            schema == SchemaVersion.V1
            and url_cache_data.max_clicks is not None
            and url_cache_data.total_clicks >= url_cache_data.max_clicks
        ):
            log.info(
                "url_resolve_expired_max_clicks",
                short_code=short_code,
                total_clicks=url_cache_data.total_clicks,
                max_clicks=url_cache_data.max_clicks,
            )
            raise GoneError("URL has expired (max clicks reached)")

        # 4c. Raise for URLs whose expiration time has passed (any schema)
        await self._raise_if_time_expired(url_cache_data, schema, short_code, "db")

        return url_cache_data, schema

    async def _raise_if_time_expired(
        self, data: UrlCacheData, schema: SchemaVersion, short_code: str, source: str
    ) -> None:
        """Enforce time-based expiry at resolve time. v2: persist the flip
        (mirrors max-clicks, ``expire_if_time_reached``) then raise; v1/emoji
        have no status field — lazy-raise forever.

        Post-flip reads stay O(1): the DB branch recaches the link as
        minimal EXPIRED on the next resolve (cache populate runs before
        the non-ACTIVE raise) — do NOT "optimize" that recache away.
        """
        if not data.is_time_expired(time.time()):
            return
        log.info(
            "url_resolve_expired_time",
            short_code=short_code,
            schema=schema,
            source=source,
        )
        if schema == SchemaVersion.V2 and data.url_status == UrlStatus.ACTIVE:
            flipped = await self._url_repo.expire_if_time_reached(ObjectId(data.id))
            if flipped:
                log.info(
                    "url_expired",
                    url_id=data.id,
                    short_code=short_code,
                    reason="expiration_time_reached",
                )
                await self._url_cache.invalidate(short_code, data.domain)
                # link.expired fires at discovery, once per link — the
                # atomic flip above is the gate. One extra read on a
                # once-per-link branch buys the full snapshot payload.
                doc = await self._url_repo.find_by_id(ObjectId(data.id))
                if doc is not None:
                    event = build_link_expired(doc, "time_expired")
                    if event is not None:
                        await self._events.emit(event)
        raise GoneError("URL has expired (expiration time reached)")

    async def check_alias_available(
        self, alias: str, *, domain: str | None = None
    ) -> bool:
        """Return True if alias is free under the given domain namespace.

        When ``domain`` is explicitly the system default (or omitted), also
        checks the legacy ``urls`` collection — v1 alias collisions still
        matter on the original namespace. Custom domains only check v2 since
        v1/emoji predate per-domain scoping and never live on custom
        hostnames.
        """
        target_domain = domain or self._system_default_domain
        if await self._url_repo.check_alias_exists(alias, target_domain):
            return False
        if target_domain == self._system_default_domain:
            if await self._legacy_repo.check_exists(alias):
                return False
            # Legacy emoji _ids may carry historically-accepted variation
            # selectors; a legacy ⭐️ must block a new canonical ⭐ or the
            # v2-first resolve order would shadow the live legacy link.
            if is_emoji_candidate(alias):
                return not await self._emoji_repo.check_exists_vs16_insensitive(alias)
        return True

    async def check_alias(
        self, alias: str, *, domain: str | None = None
    ) -> AliasCheckResult:
        """Evaluate a candidate alias against the full creation rules.

        Mirrors what POST /api/v1/shorten would enforce (length, charset,
        emoji policy, reserved words, collision) so the UI can surface
        precise feedback without duplicating the rules. Returns a single
        literal describing the first failing check, or ``"available"`` when
        the alias would be accepted today.
        """
        if is_emoji_candidate(alias):
            canonical = canonicalize_emoji_alias(alias)
            if not is_emoji_only_shape(canonical):
                return "format"
            verdict = check_emoji_alias(
                canonical,
                max_graphemes=self._max_emoji_alias_length,
                max_version=self._emoji_accept_max_version,
            )
            if verdict in ("length", "empty"):
                return "length"
            if verdict != "ok":
                return "emoji_policy"
            if not await self.check_alias_available(canonical, domain=domain):
                return "taken"
            return "available"
        if not (3 <= len(alias) <= 16):
            return "length"
        if not validate_alias(alias):
            return "format"
        target_domain = domain or self._system_default_domain
        if target_domain == self._system_default_domain and is_reserved_alias(alias):
            return "reserved"
        if not await self.check_alias_available(alias, domain=domain):
            return "taken"
        return "available"

    async def get_owned(self, url_id: ObjectId, owner_id: ObjectId) -> UrlV2Doc:
        """Fetch a single URL by ObjectId, scoped to its owner.

        Ownership is enforced IN the query — a foreign id answers exactly
        like a missing one (404, no existence oracle). Status-blind for the
        owner: expired/disabled/blocked links return with their status
        field rather than raising, since the owner is reading their own
        inventory, not following a redirect.

        Raises:
            NotFoundError: URL doesn't exist or belongs to someone else.
        """
        doc = await self._url_repo.find_by_id_and_owner(url_id, owner_id)
        if doc is None:
            raise NotFoundError("URL not found")
        return doc

    async def get_owned_by_alias(
        self, alias: str, owner_id: ObjectId, *, domain: str | None = None
    ) -> UrlV2Doc:
        """Fetch a single URL by its natural key ``(domain, alias)``, scoped
        to its owner.

        ``domain`` of None means the system default namespace. v2-only by
        construction — the ``urlsV2`` collection is all this reads, so
        v1/emoji legacy shorts never answer here. Same ownership-in-query
        and status-blind semantics as ``get_owned``.

        Raises:
            NotFoundError: no owned URL under that (domain, alias).
        """
        target_domain = domain or self._system_default_domain
        doc = await self._url_repo.find_by_alias_and_owner(
            alias, target_domain, owner_id
        )
        if doc is None:
            raise NotFoundError("URL not found")
        return doc

    async def resolve_meta_image(
        self, meta: MetaTagsRequest, owner_id: ObjectId
    ) -> tuple[MetaTagsRequest, dict | None]:
        """Resolve a data-URI image to its R2-hosted URL.

        Returns the (possibly rewritten) request plus synchronous
        image_meta for uploads; https URLs pass through untouched (the
        async validator checks them out-of-band).
        """
        if not meta.image:
            return meta, None
        ingested = await ingest_meta_image(
            meta.image,
            owner_id=owner_id,
            storage=self._r2_storage,
            max_bytes=self._meta_image_max_bytes,
            key_secret=self._meta_key_secret,
        )
        if ingested.r2_hosted:
            return meta.model_copy(update={"image": ingested.url}), ingested.image_meta
        return meta, None

    async def validate_meta_tags(self, meta: MetaTagsRequest, *, long_url: str) -> None:
        """Abuse checks for a meta_tags write.

        Blocklist regexes run over title/description/image (the same
        patterns the long_url flows through at create), plus a destination
        re-check — a link that was fine as a bare redirect deserves a second
        look the moment someone dresses it up with a custom preview.
        """
        patterns = await self._blocked_url_repo.get_patterns()
        # Off the event loop, matching geo's blocklist validation (d4a18fa):
        # a fixed set of fields now, but the blocklist grows as the abuse
        # strategy matures, so keep the regex scans off the hot loop.
        await asyncio.to_thread(
            self._scan_meta_blocklist, meta, long_url=long_url, patterns=patterns
        )

    def _scan_meta_blocklist(
        self, meta: MetaTagsRequest, *, long_url: str, patterns: Sequence[str]
    ) -> None:
        for value in (meta.title, meta.description, meta.image):
            if value and not validate_blocked_url(
                value, patterns, timeout=self._blocked_url_regex_timeout
            ):
                log.info("meta_tags_rejected", reason="blocked_pattern")
                raise ValidationError(
                    "meta_tags content is not allowed", field="meta_tags"
                )
        if not validate_blocked_url(
            long_url, patterns, timeout=self._blocked_url_regex_timeout
        ):
            log.info("meta_tags_rejected", reason="blocked_destination")
            raise ValidationError("URL is blocked", field="long_url")

    async def create(
        self,
        request: CreateUrlRequest,
        owner_id: ObjectId | None,
        client_ip: str,
        *,
        domain: str | None = None,
        created_via: str | None = None,
    ) -> tuple[UrlV2Doc, str | None]:
        """
        Create a new shortened URL.

        ``domain`` scopes the new URL to a tenant. None or omitted defaults to
        the system default. Callers MUST validate domain ownership + ACTIVE
        status before calling — service treats the value as opaque.

        ``created_via`` is the parsed X-Spoo-Client slug (see
        shared.client_tag) — callers pass the validated value, never the
        raw header.

        Returns:
            ``(doc, claim_token)`` — claim_token only for anonymous creates
            (None otherwise); the plaintext exists only in this return.

        Raises:
            ValidationError: URL is invalid, blocked, or field validation fails.
            ConflictError:   The requested alias is already taken.
        """
        target_domain = domain or self._system_default_domain
        now = datetime.now(timezone.utc)

        # 1+2. The L0 gate: format + self-link + every registered policy
        # provider (operator patterns, threat feeds). Precise reason is
        # logged by the gate; the wire message stays coarse for security
        # rejections.
        rejection = await self._url_policy.check(request.long_url)
        if rejection is not None:
            log.info("url_create_rejected", reason=rejection.code)
            raise ValidationError(rejection.public_message, field="long_url")

        # 2b. Every geo destination runs the FULL gate, same as long_url.
        if request.geo_rules:
            _validate_geo_rules_shape(
                request.geo_rules,
                max_countries=self._geo_rules_max_countries,
                enabled=self._geo_rules_enabled,
            )
            for geo_code, geo_url in request.geo_rules.items():
                geo_rejection = await self._url_policy.check(geo_url)
                if geo_rejection is not None:
                    log.info("url_create_rejected", reason=geo_rejection.code)
                    raise ValidationError(
                        geo_rejection.public_message, field=f"geo_rules.{geo_code}"
                    )
        # Meta-tags: resolve data-URI uploads to R2 URLs, then run abuse
        # checks (the route layer already gated the feature itself).
        meta_req = request.meta_tags
        meta_image_meta: dict | None = None
        if meta_req:
            meta_req, meta_image_meta = await self.resolve_meta_image(
                meta_req, owner_id if owner_id is not None else ANONYMOUS_OWNER_ID
            )
            await self.validate_meta_tags(meta_req, long_url=request.long_url)

        # 3. Password hash (cheap — do before alias generation loop)
        password_hash: str | None = None
        if request.password:
            password_hash = hash_password(request.password)

        # 4. expire_after (already parsed to datetime by the DTO validator)
        expire_ts: datetime | None = request.expire_after
        if expire_ts is not None and expire_ts <= now:
            raise ValidationError(
                "expire_after must be in the future", field="expire_after"
            )

        # 5. Alias — generate or validate custom (may loop; done after cheap checks)
        if request.alias:
            alias = self._validate_and_canonicalize_custom_alias(request.alias)
            # Reserved words only shadow paths on the default domain —
            # custom-domain namespaces carry no frontend routes.
            if target_domain == self._system_default_domain and is_reserved_alias(
                alias
            ):
                log.info("url_alias_reserved", short_code=alias)
                raise ValidationError("Alias is reserved", field="alias")
            if not await self.check_alias_available(alias, domain=target_domain):
                log.info("url_alias_conflict", short_code=alias)
                raise ConflictError("Alias is already in use")
        elif request.alias_type == "emoji":
            alias = await self._generate_unique_emoji_alias(domain=target_domain)
        else:
            alias = await self._generate_unique_alias(domain=target_domain)

        # 6. private_stats default depends on auth state
        private_stats: bool | None = request.private_stats
        if private_stats is None:
            private_stats = True if owner_id is not None else None

        # 7. Build document model (validates fields via Pydantic)
        owner_oid = owner_id if owner_id is not None else ANONYMOUS_OWNER_ID
        # Anonymous creates mint a one-time claim token; only the hash is
        # stored, the raw value is returned once and never logged.
        claim_token: str | None = None
        if owner_oid == ANONYMOUS_OWNER_ID:
            claim_token = generate_secure_token()
        url_doc = UrlV2Doc(
            alias=alias,
            owner_id=owner_oid,
            claim_token_hash=hash_token(claim_token) if claim_token else None,
            domain=target_domain,
            created_at=now,
            creation_ip=client_ip,
            created_via=created_via,
            long_url=request.long_url,
            dest=UrlDestination.from_url(request.long_url),
            password=password_hash,
            block_bots=request.block_bots,
            max_clicks=request.max_clicks,
            expire_after=expire_ts,
            geo_rules=request.geo_rules or None,
            status=UrlStatus.ACTIVE,
            private_stats=private_stats,
            total_clicks=0,
            last_click=None,
            meta_tags=(
                LinkMetaTags(
                    title=meta_req.title,
                    description=meta_req.description,
                    image=meta_req.image,
                    color=meta_req.color,
                    image_meta=meta_image_meta,
                    updated_at=now,
                    updated_ip=client_ip,
                )
                if meta_req
                else None
            ),
        )
        doc = url_doc.model_dump(by_alias=True, exclude={"id"})
        # These fields must be ABSENT (not null) when unset: the claim pair
        # feeds the owner_claimed partial index ($exists matches nulls) and
        # dest feeds the sparse dest_registrable index.
        for _absent_if_none in ("claim_token_hash", "claimed_at", "dest"):
            if doc.get(_absent_if_none) is None:
                doc.pop(_absent_if_none, None)

        # 8. Insert
        inserted_id = await self._url_repo.insert(doc)
        url_doc.id = inserted_id

        # L1 accumulation: count the successful create (best-effort, the
        # policy service never raises from here).
        await self._url_policy.record_create(request.long_url)

        _url_base = request.long_url.split("?")[0]
        _log_url = f"{_url_base}?[REDACTED]" if "?" in request.long_url else _url_base

        log.info(
            "url_created",
            short_code=alias,
            long_url=_log_url,
            long_url_domain=extract_hostname(request.long_url),
            user_id=str(owner_id) if owner_id else None,
            schema=SchemaVersion.V2,
            has_password=bool(password_hash),
            max_clicks=request.max_clicks,
            block_bots=request.block_bots,
            has_expiration=bool(expire_ts),
            private_stats=private_stats,
            alias_custom=bool(getattr(request, "alias", None)),
            domain=target_domain,
            geo_rules=len(request.geo_rules or {}),
            has_meta_tags=bool(request.meta_tags),
        )

        # Edge-first: push the prerendered OG page to KV so preview bots
        # are answered at the edge. Best-effort — never fails the write.
        if url_doc.meta_tags and self._og_writethrough:
            await self._og_writethrough.sync(UrlCacheData.from_v2_doc(url_doc))

        # External https images get validated out-of-band (uploads carried
        # image_meta already). Best-effort emit — sink swallows failures.
        await self._maybe_emit_image_validation(url_doc)

        await self._emit_link_event("link.created", url_doc)

        return url_doc, claim_token

    async def claim(
        self, claims: Sequence[ClaimItemRequest], owner_id: ObjectId
    ) -> list[ClaimResult]:
        """Claim anonymously-created URLs into *owner_id*'s account.

        Per-item forgiving, never a batch failure. Unknown ids, foreign
        owners, wrong tokens, and unclaimable links all collapse into the
        same ``invalid`` — no existence oracle. A successful claim is one
        CAS write (owner + claimed_at stamped, hash burned) plus a
        redirect-cache eviction; clicks are never touched.

        Raises:
            ForbiddenError: the batch would push the account past the
                per-account claim ceiling (conservative: counts the whole
                batch before per-item outcomes are known). The check is
                read-then-act by design: concurrent batches can overshoot
                by at most a batch each, acceptable for a soft product
                bound sitting far under the read-side backstop.
        """
        already_claimed = await self._url_repo.count_claimed(owner_id)
        if already_claimed + len(claims) > CLAIM_LIMIT_PER_ACCOUNT:
            log.info(
                "url_claim_ceiling_hit",
                user_id=str(owner_id),
                claimed=already_claimed,
                batch=len(claims),
            )
            raise ForbiddenError(
                f"Claim limit reached: an account can claim up to "
                f"{CLAIM_LIMIT_PER_ACCOUNT} links"
            )

        now = datetime.now(timezone.utc)
        results: list[ClaimResult] = []
        for item in claims:
            url_id = ObjectId(item.url_id)
            existing = await self._url_repo.find_by_id(url_id)
            if existing is None:
                results.append(ClaimResult(item.url_id, "invalid"))
                continue
            if existing.owner_id == owner_id:
                # Idempotent repeat; token deliberately not re-checked.
                results.append(ClaimResult(item.url_id, "already_yours"))
                continue
            if existing.owner_id != ANONYMOUS_OWNER_ID or not existing.claim_token_hash:
                # Don't distinguish between wrong token / already claimed /
                # not found to avoid oracle attacks.
                results.append(ClaimResult(item.url_id, "invalid"))
                continue
            token_hash = hash_token(item.token)
            if not hmac.compare_digest(token_hash, existing.claim_token_hash):
                results.append(ClaimResult(item.url_id, "invalid"))
                continue
            swapped = await self._url_repo.claim_by_token_hash(
                url_id, token_hash, owner_id, now
            )
            if not swapped:
                # Lost a concurrent race — whoever won owns it now.
                results.append(ClaimResult(item.url_id, "invalid"))
                continue
            # Evict so the next click stamps the new owner.
            await self._url_cache.invalidate(existing.alias, existing.domain)
            log.info(
                "url_claimed",
                url_id=item.url_id,
                short_code=existing.alias,
                domain=existing.domain,
                user_id=str(owner_id),
            )
            # Deliberately no webhook: a claim is actor-caused but has no
            # honest expression in the event contract (ownership is not a
            # payload field), and the catalog is forever. Revisit only if
            # an integration proves the need.
            results.append(ClaimResult(item.url_id, "claimed"))
        return results

    async def update(
        self,
        url_id: ObjectId,
        request: UpdateUrlRequest,
        owner_id: ObjectId,
        client_ip: str | None = None,
    ) -> UrlV2Doc:
        """
        Update an existing URL.

        EXPIRED URLs are auto-reactivated when expiry conditions change
        (max_clicks raised/cleared, expire_after extended/cleared), unless
        the caller also provides an explicit status override.

        ``client_ip`` is stamped onto ``meta_tags.updated_ip`` when the
        request writes meta_tags — abuse forensics for preview edits.

        Raises:
            NotFoundError:  URL doesn't exist.
            ForbiddenError: Caller doesn't own the URL, or URL is blocked.
            ConflictError:  Requested alias is already taken.
            ValidationError: Invalid field values.
        """
        now = datetime.now(timezone.utc)

        # 1. Load existing document
        existing = await self._url_repo.find_by_id(url_id)
        if existing is None:
            raise NotFoundError("URL not found")

        # 2. Ownership check
        if existing.owner_id != owner_id:
            raise ForbiddenError("Access denied: you do not own this URL")

        # 2b. Admin-blocked URLs cannot be modified by the owner
        if existing.status == UrlStatus.BLOCKED:
            raise ForbiddenError("Cannot modify a blocked URL")

        # 3. Build update ops via field handlers
        update_ops: dict = {}
        for handler in FIELD_HANDLERS.values():
            await handler(request, existing, update_ops, self)

        # Handlers don't see the request context; stamp the writer's IP
        # onto a fresh meta_tags value here (None stays None on clears).
        if update_ops.get("meta_tags"):
            update_ops["meta_tags"]["updated_ip"] = client_ip

        # Auto-reactivate EXPIRED URLs when expiry conditions improve
        self._auto_reactivate(existing, update_ops, now)

        if not update_ops:
            return existing  # No changes detected

        update_ops["updated_at"] = now

        # 4. Persist. A None dest is unset: absent-not-null parity with create.
        update_doc: dict = {"$set": update_ops}
        if "dest" in update_ops and update_ops["dest"] is None:
            update_doc = {
                "$set": {k: v for k, v in update_ops.items() if k != "dest"},
                "$unset": {"dest": ""},
            }
        await self._url_repo.update(url_id, update_doc)

        # 5. Invalidate cache. Always clear the pre-change (alias, domain) so a
        # rename or move can't be served stale from the old key. When the new
        # key differs (alias rename and/or domain move), clear that too —
        # belt-and-suspenders against a racing populate from another worker
        # that filled the cache between our read and persist.
        await self._url_cache.invalidate(existing.alias, existing.domain)
        new_alias = update_ops.get("alias", existing.alias)
        new_domain = update_ops.get("domain", existing.domain)
        if (new_alias, new_domain) != (existing.alias, existing.domain):
            await self._url_cache.invalidate(new_alias, new_domain)

        log.info(
            "url_updated",
            url_id=str(url_id),
            short_code=existing.alias,
            user_id=str(owner_id),
            fields_changed=list(update_ops.keys()),
        )
        if "domain" in update_ops:
            log.info(
                "url_domain_moved",
                url_id=str(url_id),
                short_code=new_alias,
                from_domain=existing.domain,
                to_domain=update_ops["domain"],
                user_id=str(owner_id),
            )

        # Return merged doc (avoids extra DB round-trip)
        merged = existing.model_dump(by_alias=True)
        merged.update(update_ops)
        merged["_id"] = url_id
        merged_doc = UrlV2Doc.from_mongo(merged)

        # Edge KV write-through — only for links that have meta_tags now or
        # had them before this write (plain links' promoted redirect entries
        # must never be touched). A key change (alias/domain move) drops the
        # old entry; sync() re-puts or deletes under the new key.
        is_og = existing.meta_tags is not None or merged_doc.meta_tags is not None
        if self._og_writethrough and is_og:
            relevant = {"meta_tags", "long_url", "status", "alias", "domain"}
            if relevant & update_ops.keys():
                if (new_alias, new_domain) != (existing.alias, existing.domain):
                    await self._og_writethrough.remove(existing.domain, existing.alias)
                await self._og_writethrough.sync(UrlCacheData.from_v2_doc(merged_doc))
        elif not is_og and (
            update_ops.get("status") == UrlStatus.INACTIVE
            or (new_alias, new_domain) != (existing.alias, existing.domain)
        ):
            # Plain links: deactivation and key changes must drop any
            # hot-promoted entry under the pre-change key (takedown parity
            # with the bulk endpoints; og-links are handled above). The
            # new key needs nothing — a link is never promoted under a
            # key it hasn't served on.
            self._purge_edge_key(existing.domain, existing.alias)

        if "meta_tags" in update_ops:
            await self._maybe_emit_image_validation(merged_doc)

        await self._emit_link_event(
            "link.updated",
            merged_doc,
            extra={"changes": event_changes(existing, update_ops)},
        )

        return merged_doc

    async def _emit_link_event(
        self, event_type: str, doc: UrlV2Doc, extra: dict | None = None
    ) -> None:
        """Publish a link lifecycle fact to the domain-event backbone.

        Anonymous links skip entirely (no possible webhook subscriber);
        the sink never raises, so producers stay unconditional.
        """
        owner = link_owner_id(doc)
        if owner is None:
            return
        data: dict = {"link": link_snapshot(doc)}
        if extra:
            data.update(extra)
        await self._events.emit(DomainEvent(type=event_type, owner_id=owner, data=data))

    async def _maybe_emit_image_validation(self, doc: UrlV2Doc) -> None:
        """Queue async validation for an external https og:image."""
        meta = doc.meta_tags
        if (
            self._meta_image_sink is None
            or meta is None
            or not meta.image
            or meta.image_meta is not None  # upload — validated synchronously
        ):
            return
        await self._meta_image_sink.emit(
            MetaImageValidateEvent(
                url_id=str(doc.id),
                alias=doc.alias,
                domain=doc.domain,
                image_url=meta.image,
            )
        )

    def _purge_edge_key(self, domain: str, alias: str) -> None:
        """Fire-and-forget drop of a plain link's hot-promoted edge entry.

        Takedown parity with the bulk endpoints: a deleted/deactivated/
        renamed link must stop serving at the edge now, not when the
        promotion TTL runs out — and a freed alias must not keep serving
        the previous owner's destination. og-links are event-managed by
        the write-through instead; callers gate on meta_tags so the two
        mechanisms don't double-fire. Purge-only (a dropped entry just
        re-promotes next hot window), best-effort, never awaited in the
        request, and a no-op for tenant links (never edge-cached) or
        when the edge cache is unconfigured.
        """
        if self._edge_kv is None or domain != self._system_default_domain:
            return
        task = asyncio.create_task(self._purge_edge_key_logged(domain, alias))
        self._edge_purge_tasks.add(task)
        task.add_done_callback(self._edge_purge_tasks.discard)

    async def _purge_edge_key_logged(self, domain: str, alias: str) -> None:
        """Body of the detached purge — failures are logged with takedown
        context instead of dying as unretrieved-task noise. The KV client
        itself never raises (bool contract), so the except arm only
        catches genuine bugs."""
        try:
            ok = await self._edge_kv.delete(cache_key(domain, alias))
            if not ok:
                log.warning("edge_purge_failed", short_code=alias, domain=domain)
        except Exception:
            log.exception("edge_purge_failed", short_code=alias, domain=domain)

    def _auto_reactivate(
        self, existing: UrlV2Doc, update_ops: dict, now: datetime
    ) -> None:
        """Reactivate an EXPIRED URL if the REQUESTED CHANGE improves an
        expiry condition — every arm requires its field in ``update_ops``
        so ambient values can't resurrect the URL (an unrelated edit to a
        max-clicks-expired link with a future expire_after must not
        reactivate it).

        Only applies when the URL is currently EXPIRED and the caller
        did not explicitly set a new status.
        """
        if existing.status != UrlStatus.EXPIRED:
            return
        if "status" in update_ops:
            return

        new_max = update_ops.get("max_clicks", existing.max_clicks)
        new_expire = update_ops.get("expire_after", existing.expire_after)

        max_clicks_cleared = "max_clicks" in update_ops and new_max is None
        max_clicks_raised = (
            "max_clicks" in update_ops
            and new_max is not None
            and new_max > existing.total_clicks
        )
        expire_extended = (
            "expire_after" in update_ops and new_expire is not None and new_expire > now
        )
        expire_cleared = "expire_after" in update_ops and new_expire is None

        if max_clicks_cleared or max_clicks_raised or expire_extended or expire_cleared:
            update_ops["status"] = UrlStatus.ACTIVE

    async def delete(
        self,
        url_id: ObjectId,
        owner_id: ObjectId,
    ) -> None:
        """
        Delete a URL.

        Raises:
            NotFoundError:  URL doesn't exist.
            ForbiddenError: Caller doesn't own the URL, or URL is blocked.
        """
        existing = await self._url_repo.find_by_id(url_id)
        if existing is None:
            raise NotFoundError("URL not found")

        if existing.owner_id != owner_id:
            raise ForbiddenError("Access denied: you do not own this URL")

        if existing.status == UrlStatus.BLOCKED:
            raise ForbiddenError("Cannot delete a blocked URL")

        await self._url_repo.delete(url_id)
        await self._url_cache.invalidate(existing.alias, existing.domain)
        if existing.meta_tags is not None and self._og_writethrough:
            await self._og_writethrough.remove(existing.domain, existing.alias)
        else:
            self._purge_edge_key(existing.domain, existing.alias)

        log.info(
            "url_deleted",
            url_id=str(url_id),
            short_code=existing.alias,
            user_id=str(owner_id),
        )
        await self._emit_link_event("link.deleted", existing)

    async def delete_all_by_domain(
        self,
        owner_id: ObjectId,
        domain: str,
    ) -> int:
        """Bulk-delete all URLs owned by *owner_id* under *domain*.

        Refuses the system default — that would nuke all of a user's spoo.me
        URLs in one call. Returns number of URLs deleted.

        Used by:
          - `DELETE /api/v1/urls?domain=` (standalone bulk delete)
          - `CustomDomainService.delete(cascade=True)` (domain revoke cascade)
        """
        if domain == self._system_default_domain:
            raise ValidationError(
                "cannot bulk-delete URLs on the system default domain",
                field="domain",
            )

        aliases = await self._url_repo.list_aliases_by_owner_and_domain(
            owner_id, domain
        )
        if not aliases:
            return 0

        deleted = await self._url_repo.delete_many_by_owner_and_domain(owner_id, domain)

        # Best-effort cache cleanup; cache miss after delete is correct anyway.
        await self._url_cache.invalidate_many(aliases, domain)

        log.info(
            "urls_bulk_deleted",
            user_id=str(owner_id),
            domain=domain,
            count=deleted,
        )
        return deleted

    async def list_by_owner(
        self,
        owner_id: ObjectId,
        query: ListUrlsQuery,
    ) -> dict:
        """Return a paginated list of URLs owned by this user.

        Returns a dict with ``items`` as a list of ``UrlV2Doc`` domain
        objects (not DTOs).  The route layer must map items to
        ``UrlListItem.from_doc()`` before returning to clients.
        """
        start_time = time.perf_counter()
        mongo_query: dict = {"owner_id": owner_id}

        if getattr(query, "domain", None):
            mongo_query["domain"] = query.domain

        f = query.parsed_filter

        # Compound clauses (status, search) may each carry their own $or —
        # collect them and compose via $and so they can't clobber each other.
        and_clauses: list[dict] = []

        if f:
            if f.status:
                and_clauses.append(
                    effective_status_clause(f.status, datetime.now(timezone.utc))
                )

            date_range: dict = {}
            if f.created_after:
                dt = parse_datetime(f.created_after)
                if dt:
                    date_range["$gte"] = dt
            if f.created_before:
                dt = parse_datetime(f.created_before)
                if dt:
                    date_range["$lte"] = dt
            if date_range:
                mongo_query["created_at"] = date_range

            if f.password_set is True:
                mongo_query["password"] = {"$ne": None}
            elif f.password_set is False:
                mongo_query["password"] = None

            if f.max_clicks_set is True:
                mongo_query["max_clicks"] = {"$ne": None}
            elif f.max_clicks_set is False:
                mongo_query["max_clicks"] = None

            if f.search:
                try:
                    pattern = re.compile(re.escape(f.search), re.IGNORECASE)
                    and_clauses.append(
                        {"$or": [{"alias": pattern}, {"long_url": pattern}]}
                    )
                except re.error:
                    raise ValidationError(
                        "Invalid search pattern", field="filter.search"
                    ) from None

        if len(and_clauses) == 1:
            mongo_query.update(and_clauses[0])
        elif and_clauses:
            mongo_query["$and"] = and_clauses

        sort_order = (
            -1 if query.sort_order.lower() in ("desc", "descending", "-1") else 1
        )
        skip = (query.page - 1) * query.page_size

        total = await self._url_repo.count_by_query(mongo_query)
        docs = await self._url_repo.find_by_owner(
            query=mongo_query,
            sort_field=query.sort_by,
            sort_order=sort_order,
            skip=skip,
            limit=query.page_size,
        )

        has_next = (skip + len(docs)) < total
        duration_ms = int((time.perf_counter() - start_time) * 1000)

        log.info(
            "url_list_query",
            user_id=str(owner_id),
            page=query.page,
            page_size=query.page_size,
            sort_by=query.sort_by,
            sort_order="descending" if sort_order == -1 else "ascending",
            filter_count=len(mongo_query) - 1,  # subtract the base owner_id filter
            total=total,
            returned=len(docs),
            has_next=has_next,
            duration_ms=duration_ms,
        )

        return {
            "items": docs,
            "page": query.page,
            "pageSize": query.page_size,
            "total": total,
            "hasNext": has_next,
            "sortBy": query.sort_by,
            "sortOrder": "descending" if sort_order == -1 else "ascending",
        }

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _dispatch(
        self, short_code: str, raw_code: str | None = None
    ) -> tuple[UrlCacheData | None, SchemaVersion]:
        """
        Determine URL schema and fetch from the appropriate collection.

        The lookup order comes from ``shared.alias_dispatch.resolution_order``
        — the single source of truth shared with the public preview endpoint,
        so every resolving surface answers a given code from the same
        generation. Only the per-generation lookup/conversion lives here.

        ``short_code`` is the canonical lookup form (v2 + cache key);
        ``raw_code`` is the as-requested form, needed for exact legacy
        ``emojis`` ``_id`` matches (defaults to ``short_code``).
        """
        raw = raw_code if raw_code is not None else short_code
        order = resolution_order(raw)
        if SchemaVersion.EMOJI in order:
            # v2 first — new emoji links live in urlsV2 under the canonical
            # alias. Creation collision-checks against the legacy collection
            # (VS16-insensitively), so a v2 hit can never shadow a live
            # legacy variant.
            v2_doc = await self._url_repo.find_by_alias(
                short_code, self._system_default_domain
            )
            if v2_doc is not None:
                return UrlCacheData.from_v2_doc(v2_doc), SchemaVersion.V2
            for candidate in emoji_lookup_candidates(raw):
                doc = await self._emoji_repo.find_by_id(candidate)
                if doc is not None:
                    return (
                        _emoji_doc_to_cache(
                            candidate, doc, self._system_default_domain
                        ),
                        SchemaVersion.EMOJI,
                    )
            return None, SchemaVersion.EMOJI
        if order[0] == SchemaVersion.V1:
            return await self._try_v1_then_v2(short_code)
        return await self._try_v2_then_v1(short_code)

    async def _dispatch_custom_domain(
        self, short_code: str, domain: str
    ) -> tuple[UrlCacheData | None, SchemaVersion]:
        # Custom domains are v2-only by construction.
        v2_doc = await self._url_repo.find_by_alias(short_code, domain)
        if v2_doc is None:
            return None, SchemaVersion.V2
        return UrlCacheData.from_v2_doc(v2_doc), SchemaVersion.V2

    async def _try_v2_then_v1(
        self, short_code: str
    ) -> tuple[UrlCacheData | None, SchemaVersion]:
        v2_doc = await self._url_repo.find_by_alias(
            short_code, self._system_default_domain
        )
        if v2_doc is not None:
            return UrlCacheData.from_v2_doc(v2_doc), SchemaVersion.V2
        v1_doc = await self._legacy_repo.find_by_id(short_code)
        if v1_doc is not None:
            return (
                _legacy_doc_to_cache(short_code, v1_doc, self._system_default_domain),
                SchemaVersion.V1,
            )
        return None, SchemaVersion.V2

    async def _try_v1_then_v2(
        self, short_code: str
    ) -> tuple[UrlCacheData | None, SchemaVersion]:
        v1_doc = await self._legacy_repo.find_by_id(short_code)
        if v1_doc is not None:
            return (
                _legacy_doc_to_cache(short_code, v1_doc, self._system_default_domain),
                SchemaVersion.V1,
            )
        v2_doc = await self._url_repo.find_by_alias(
            short_code, self._system_default_domain
        )
        if v2_doc is not None:
            return UrlCacheData.from_v2_doc(v2_doc), SchemaVersion.V2
        return None, SchemaVersion.V2

    async def _populate_cache(
        self,
        short_code: str,
        url_cache_data: UrlCacheData,
        schema: SchemaVersion,
    ) -> None:
        """
        Cache the URL data according to caching rules:
          - v2 (any status): cache (minimal for non-ACTIVE)
          - v1 without max-clicks: cache
          - v1 with max-clicks: do NOT cache (total-clicks must be live)
          - emoji: do NOT cache
        """
        if schema == SchemaVersion.V2 or (
            schema == SchemaVersion.V1 and url_cache_data.max_clicks is None
        ):
            await self._url_cache.set(short_code, url_cache_data)

    async def _generate_unique_alias(self, *, domain: str | None = None) -> str:
        """Generate a 7-character alias not already in urlsV2 for *domain*."""
        target_domain = domain or self._system_default_domain
        for _ in range(10):
            candidate = generate_short_code_v2(7)
            if not await self._url_repo.check_alias_exists(candidate, target_domain):
                return candidate
        log.error("url_alias_generation_exhausted", domain=target_domain)
        raise AppError("Could not generate a unique alias; please try again")

    async def _generate_unique_emoji_alias(self, *, domain: str | None = None) -> str:
        """Generate an emoji alias from the safe pool, unique for *domain*.

        Uses the full ``check_alias_available`` (not the bare v2 check) so
        candidates are also collision-checked against the legacy ``urls``
        and ``emojis`` collections on the system domain.
        """
        target_domain = domain or self._system_default_domain
        for _ in range(10):
            candidate = generate_emoji_alias_v2(
                self._emoji_generated_alias_length,
                max_version=self._emoji_generate_max_version,
            )
            if await self.check_alias_available(candidate, domain=target_domain):
                return candidate
        log.error("url_alias_generation_exhausted", domain=target_domain, kind="emoji")
        raise AppError("Could not generate a unique alias; please try again")

    def _validate_and_canonicalize_custom_alias(self, alias: str) -> str:
        """Return the canonical stored form of a custom alias, or raise.

        The one enforcement point for create AND update: alphanumeric
        aliases pass through unchanged; emoji aliases are canonicalized
        (unquote → NFC → VS16-strip) and checked against the acceptance
        policy with the configured caps.
        """
        if validate_alias(alias):
            return alias  # pure alphanumeric — DTO already bounded 3-16
        canonical = canonicalize_emoji_alias(alias)
        verdict = check_emoji_alias(
            canonical,
            max_graphemes=self._max_emoji_alias_length,
            max_version=self._emoji_accept_max_version,
        )
        if verdict == "length":
            raise ValidationError(
                f"emoji alias must be 1-{self._max_emoji_alias_length} emoji",
                field="alias",
            )
        if verdict != "ok":
            raise ValidationError(
                "alias contains unsupported emoji or characters", field="alias"
            )
        return canonical


# ── Module-level helpers ──────────────────────────────────────────────────────


def _raise_for_status(status: UrlStatus) -> None:
    if status == UrlStatus.BLOCKED:
        raise BlockedUrlError("URL is blocked")
    raise GoneError("URL has expired or is no longer active")


def _derived_expiry_arms(now: datetime) -> list[dict]:
    """Mongo arms matching ACTIVE-stored docs whose effective status is
    EXPIRED. MUST express the same predicate as
    ``UrlV2Doc.effective_status`` — pinned by test.
    """
    return [
        {"status": UrlStatus.ACTIVE, "expire_after": {"$lte": now}},
        {
            "status": UrlStatus.ACTIVE,
            "max_clicks": {"$ne": None},
            "$expr": {"$gte": ["$total_clicks", "$max_clicks"]},
        },
    ]


def effective_status_clause(status: UrlStatus, now: datetime) -> dict:
    """Mongo filter matching docs by EFFECTIVE status (see
    ``UrlV2Doc.effective_status``) — the stored field alone lags for
    ACTIVE docs whose expiry has passed but whose flip hasn't been
    observed yet.
    """
    if status is UrlStatus.EXPIRED:
        return {"$or": [{"status": UrlStatus.EXPIRED}, *_derived_expiry_arms(now)]}
    if status is UrlStatus.ACTIVE:
        return {"status": UrlStatus.ACTIVE, "$nor": _derived_expiry_arms(now)}
    return {"status": status}  # INACTIVE / BLOCKED: stored is truth


def _legacy_doc_to_cache(
    short_code: str,
    doc: LegacyUrlDoc | EmojiUrlDoc,
    system_default_domain: str,
    schema_version: SchemaVersion = SchemaVersion.V1,
) -> UrlCacheData:
    """Convert a LegacyUrlDoc or EmojiUrlDoc to UrlCacheData.

    v1/emoji shorts only exist under the system default domain — they
    predate custom domains and won't ever be created elsewhere.
    """
    # Only tz-aware stored values enforce expiry — a naive v1
    # ``expiration-time`` is ambiguous and never expires, matching
    # ``v1_is_expired`` (public pages) and ``convert_to_gmt`` (legacy
    # stats) so the redirect and the read surfaces agree on v1.
    expiration_time = None
    if doc.expiration_time is not None and doc.expiration_time.tzinfo is not None:
        expiration_time = to_unix_timestamp(doc.expiration_time)
    return UrlCacheData(
        id=short_code,
        alias=short_code,
        long_url=doc.url,
        block_bots=bool(doc.block_bots),
        password_hash=doc.password,
        expiration_time=expiration_time,
        max_clicks=doc.max_clicks,
        # v1/emoji have no status machine — the safety ``blocked`` flag is
        # the only non-ACTIVE state they can be in.
        url_status=UrlStatus.BLOCKED if doc.blocked else UrlStatus.ACTIVE,
        schema_version=schema_version,
        total_clicks=doc.total_clicks,
        owner_id=None,
        domain=system_default_domain,
    )


def _emoji_doc_to_cache(
    short_code: str, doc: EmojiUrlDoc, system_default_domain: str
) -> UrlCacheData:
    return _legacy_doc_to_cache(
        short_code, doc, system_default_domain, schema_version=SchemaVersion.EMOJI
    )
