"""
Shared resolver for the public read-only link surfaces.

Both public endpoints — the link preview (``/{code}+``) and the public
stats page (``/stats/{code}``) — must answer a given short code from the
SAME generation, derive the SAME effective status, and surface the SAME
``created_at``, or the two pages could describe two different links under
one code. This module is that single source of truth; neither service
keeps a private copy of any piece of it.

Invariants (load-bearing — preserved from the preview endpoint):
  - Resolution follows ``shared.alias_dispatch.resolution_order`` (the
    same single source of truth the redirect dispatches on) and is
    scoped to the system default domain. Custom-domain aliases resolve
    only via the redirect; the public surfaces are system-domain-only.
  - v1/emoji documents are fetched RAW — the typed ``LegacyUrlDoc``
    silently drops ``creation-date`` / ``creation-time``.
  - Derived expiry is IDENTICAL to the redirect's derivation
    (``UrlV2Doc.effective_status``): a public surface must never reveal
    a destination the redirect would refuse.
  - Everything is derived READ-ONLY — nothing here writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import unquote

from repositories.legacy.emoji_url_repository import EmojiUrlRepository
from repositories.legacy.legacy_url_repository import LegacyUrlRepository
from repositories.url_repository import UrlRepository
from schemas.models.url import SchemaVersion, UrlV2Doc
from shared.alias_dispatch import (
    emoji_lookup_candidates,
    resolution_order,
    v2_lookup_code,
)
from shared.datetime_utils import as_aware_utc, convert_to_gmt, parse_datetime

# ── Derivation helpers (generation-specific primitives) ──────────────────────


def v2_effective_status(doc: UrlV2Doc) -> str:
    """Lowercase wire adapter over ``UrlV2Doc.effective_status``.

    The derivation (time lapse + max-click exhaustion folded into
    EXPIRED) lives on the model and is shared with the redirect's
    enforcement — the public surfaces and the redirect answer from the
    same predicate by construction. Only the wire casing is decided
    here (``"active" | "inactive" | "expired" | "blocked"``, frozen by
    the public-page contract).
    """
    return doc.effective_status.value.lower()


def v1_is_expired(data: dict) -> bool:
    """Mirror the legacy stats page's expired logic (routes/legacy/
    stats.py): max-clicks reached or ``expiration-time`` passed.

    Like ``convert_to_gmt``, a timezone-naive expiration is ambiguous
    and never counts as expired; unparseable ancient values are
    treated the same rather than 500-ing a public page.
    """
    max_clicks = data.get("max-clicks")
    if max_clicks is not None:
        try:
            if int(data.get("total-clicks") or 0) >= int(max_clicks):
                return True
        except (TypeError, ValueError):
            pass

    raw_expiration = data.get("expiration-time")
    if raw_expiration is None:
        return False
    if isinstance(raw_expiration, datetime):
        expires_at = raw_expiration if raw_expiration.tzinfo else None
    else:
        try:
            expires_at = convert_to_gmt(str(raw_expiration))
        except (TypeError, ValueError):
            expires_at = None
    return expires_at is not None and expires_at.astimezone(
        timezone.utc
    ) <= datetime.now(timezone.utc)


def v1_created_at(data: dict) -> datetime | None:
    """Combine v1 ``creation-date`` + ``creation-time`` (both set at
    shorten time) into an aware datetime; either missing or unparseable
    → ``None`` — ancient rows lack them and the pages omit the line.
    """
    date = data.get("creation-date")
    time_of_day = data.get("creation-time")
    if not date or not time_of_day:
        return None
    return parse_datetime(f"{date}T{time_of_day}")


# ── The resolved link ─────────────────────────────────────────────────────────


@dataclass
class ResolvedPublicLink:
    """A short code resolved for a public read-only surface.

    Exactly one of ``v2_doc`` / ``raw_v1`` is set, matching ``schema``.
    ``raw_v1`` stays RAW so legacy-only fields survive; validate it into
    ``LegacyUrlDoc`` where typed access is needed.
    """

    schema: SchemaVersion
    alias: str
    short_url: str
    v2_doc: UrlV2Doc | None = None
    raw_v1: dict[str, Any] | None = None

    @property
    def is_v2(self) -> bool:
        return self.schema is SchemaVersion.V2

    @property
    def generation(self) -> Literal["v1", "v2"]:
        """Public wire generation — emoji collapses to ``"v1"`` (emoji
        links carry v1-shaped analytics; the wire only knows v1|v2)."""
        return "v2" if self.is_v2 else "v1"

    def effective_status(self) -> str:
        """Derived wire status (lowercase) — see ``v2_effective_status``.

        v1/emoji carry a ``blocked`` flag rather than a status machine; a
        takedown must read as blocked on every public surface.
        """
        if self.is_v2:
            return v2_effective_status(self.v2_doc)
        if (self.raw_v1 or {}).get("blocked"):
            return "blocked"
        return "expired" if v1_is_expired(self.raw_v1) else "active"

    def created_at(self) -> datetime | None:
        """Aware-UTC creation time; ``None`` when the doc never stored one."""
        if self.is_v2:
            return as_aware_utc(self.v2_doc.created_at)
        return v1_created_at(self.raw_v1)


# ── The resolver ──────────────────────────────────────────────────────────────


class PublicLinkResolver:
    """Domain-scoped, status-agnostic resolution for public surfaces.

    Status-agnostic on purpose: expired / inactive / blocked links still
    resolve (the surfaces describe them without serving them) — which is
    why this deliberately does NOT use ``UrlService.resolve`` (it raises
    on non-active statuses and its cache shape lacks ``created_at``).
    """

    def __init__(
        self,
        url_repo: UrlRepository,
        legacy_repo: LegacyUrlRepository,
        emoji_repo: EmojiUrlRepository,
        *,
        system_default_domain: str,
    ) -> None:
        self._url_repo = url_repo
        self._legacy_repo = legacy_repo
        self._emoji_repo = emoji_repo
        self._system_default_domain = system_default_domain

    async def resolve(self, short_code: str) -> ResolvedPublicLink | None:
        """Resolve *short_code*; ``None`` when it misses every generation.

        The code is percent-decoded here (emoji aliases arrive encoded,
        same as the redirect path). Generation lookup order comes from
        ``resolution_order`` — shared with the redirect so all surfaces
        answer a given code from the same generation; emoji misses never
        fall through to urls/urlsV2. Lookups are exact matches; case is
        never normalized ("/Docs" and "/docs" are different links).
        """
        code = unquote(short_code)
        for generation in resolution_order(code):
            if generation is SchemaVersion.V2:
                # Emoji codes look up under their canonical form — v2
                # stores canonical aliases only (see shared.alias_dispatch).
                v2_doc = await self._url_repo.find_by_alias(
                    v2_lookup_code(code), self._system_default_domain
                )
                if v2_doc is not None:
                    return ResolvedPublicLink(
                        schema=SchemaVersion.V2,
                        alias=v2_doc.alias,
                        short_url=self._short_url(v2_doc.alias),
                        v2_doc=v2_doc,
                    )
            elif generation is SchemaVersion.EMOJI:
                # Raw form first (exact legacy semantics), then canonical —
                # same candidate order as the redirect's _dispatch.
                for candidate in emoji_lookup_candidates(code):
                    raw = await self._find_v1_raw(self._emoji_repo, candidate)
                    if raw is not None:
                        return ResolvedPublicLink(
                            schema=generation,
                            alias=candidate,
                            short_url=self._short_url(candidate),
                            raw_v1=raw,
                        )
            else:
                raw = await self._find_v1_raw(self._legacy_repo, code)
                if raw is not None:
                    return ResolvedPublicLink(
                        schema=generation,
                        alias=code,
                        short_url=self._short_url(code),
                        raw_v1=raw,
                    )
        return None

    def _short_url(self, alias: str) -> str:
        return f"https://{self._system_default_domain}/{alias}"

    @staticmethod
    async def _find_v1_raw(
        repo: LegacyUrlRepository | EmojiUrlRepository, short_code: str
    ) -> dict | None:
        """Fetch a v1/emoji document as a RAW dict by exact ``_id`` match.

        Not ``find_by_id``: that returns a typed ``LegacyUrlDoc``, which
        silently drops ``creation-date`` / ``creation-time`` (the model
        doesn't declare them and pydantic ignores extras) — and both
        public surfaces need them for ``created_at``. The public
        ``aggregate`` helper is how the legacy stats page reads raw v1
        docs too.
        """
        return await repo.aggregate([{"$match": {"_id": short_code}}])
