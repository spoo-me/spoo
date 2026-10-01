"""
Public link preview service — GET /api/v1/public/preview/{short_code}.

Resolves a short code across both URL generations WITHOUT status gating
(expired / inactive / blocked links still answer; only truly missing codes
404) and derives the preview wire shape. The safety semantic the page is
built on: the destination (and geo rules) ride the wire ONLY while the
link is active and not password-protected — the preview never reveals a
destination the redirect would refuse to serve. For geo-targeted links
that means enumerating the full country→destination map while a single
redirect follows only one rule: deliberate anti-cloaking transparency,
not a leak.

Resolution, raw v1/emoji reads, derived effective status, and created_at
all come from ``services.public_link_resolver`` — the single source of
truth shared with the public stats endpoint, so the two public surfaces
can never disagree about which link a code names or what state it is in.

Everything is derived READ-ONLY — this endpoint never writes.
"""

from __future__ import annotations

from errors import NotFoundError
from infrastructure.logging import get_logger
from schemas.dto.responses.public_preview import (
    PreviewDestination,
    PreviewGeoDestination,
    PreviewVariantDestination,
    PublicPreviewResponse,
)
from schemas.models.base import ANONYMOUS_OWNER_ID
from services.entitlements import EntitlementService
from services.feature_flag_service import DOMAIN_POLISH_FLAG, FeatureFlagService
from services.public_link_resolver import PublicLinkResolver, ResolvedPublicLink
from shared.datetime_utils import to_unix_timestamp
from shared.url_utils import split_destination

log = get_logger(__name__)


class PublicPreviewService:
    """Derivation of the public link preview wire over the shared resolver."""

    def __init__(
        self,
        resolver: PublicLinkResolver,
        entitlements: EntitlementService,
        flags: FeatureFlagService,
    ) -> None:
        self._resolver = resolver
        self._entitlements = entitlements
        self._flags = flags

    # ── Public API ────────────────────────────────────────────────────────

    async def get_preview(self, short_code: str) -> PublicPreviewResponse:
        """Resolve *short_code* (status-agnostic) and build the preview.

        Raises:
            NotFoundError: the code exists in neither generation.
        """
        link = await self._resolver.resolve(short_code)
        if link is None:
            log.info("public_preview_not_found", short_code=short_code)
            raise NotFoundError("short_code not found")
        preview = self._v2_preview(link) if link.is_v2 else self._v1_preview(link)
        owner = link.v2_doc.owner_id if link.is_v2 else None
        if not owner or owner == ANONYMOUS_OWNER_ID:
            return preview
        plan = await self._entitlements.resolve_for(owner)
        whitelabel = plan.has(
            "domain_polish"
        ) and await self._flags.is_enabled_for_owner(DOMAIN_POLISH_FLAG, owner)
        return preview.model_copy(update={"whitelabel": whitelabel})

    # ── v2 derivation ─────────────────────────────────────────────────────

    def _v2_preview(self, link: ResolvedPublicLink) -> PublicPreviewResponse:
        doc = link.v2_doc
        status = link.effective_status()
        password_protected = bool(doc.password)

        # Destination-only-while-active: password, expiry, pause and block
        # all withhold it (time-sensitive links stay dead, blocked
        # destinations stay unreachable). There is no password unlock here.
        destination: PreviewDestination | None = None
        geo_destinations: list[PreviewGeoDestination] | None = None
        variant_destinations: list[PreviewVariantDestination] | None = None
        if status == "active" and not password_protected:
            destination = PreviewDestination(**split_destination(doc.long_url))
            geo_destinations = self._group_geo_rules(doc.geo_rules)
            if doc.ab_variants:
                variant_destinations = [
                    PreviewVariantDestination(
                        weight=v.weight, **split_destination(v.url)
                    )
                    for v in doc.ab_variants
                ]
        # An expired link with a fallback still 302s everyone (the redirect
        # decides before the password gate), so the preview names that URL.
        expired_destination: PreviewDestination | None = None
        if status == "expired" and doc.expired_redirect_url:
            expired_destination = PreviewDestination(
                **split_destination(doc.expired_redirect_url)
            )

        created_at = link.created_at()
        return PublicPreviewResponse(
            generation=link.generation,
            alias=link.alias,
            short_url=link.short_url,
            status=status,
            created_at=created_at.isoformat() if created_at else None,
            starts_at=(
                to_unix_timestamp(doc.starts_at) if status == "scheduled" else None
            ),
            password_protected=password_protected,
            destination=destination,
            geo_destinations=geo_destinations,
            variant_destinations=variant_destinations,
            expired_destination=expired_destination,
        )

    @staticmethod
    def _group_geo_rules(
        geo_rules: dict[str, str] | None,
    ) -> list[PreviewGeoDestination] | None:
        """Group geo rules by destination URL, countries sorted ascending.

        Anti-cloaking rule: EVERY rule is listed, nothing summarized —
        the preview is the transparency surface. Same display grouping as
        the legacy Jinja preview (storage stays a flat code→url map).
        """
        if not geo_rules:
            return None
        grouped: dict[str, list[str]] = {}
        for country, dest in geo_rules.items():
            grouped.setdefault(dest, []).append(country)
        return [
            PreviewGeoDestination(countries=sorted(codes), **split_destination(dest))
            for dest, codes in grouped.items()
        ]

    # ── v1 / emoji derivation ─────────────────────────────────────────────

    def _v1_preview(self, link: ResolvedPublicLink) -> PublicPreviewResponse:
        data = link.raw_v1
        status = link.effective_status()
        password_protected = bool(data.get("password"))

        destination: PreviewDestination | None = None
        if status == "active" and not password_protected:
            destination = PreviewDestination(**split_destination(data.get("url") or ""))

        created_at = link.created_at()
        return PublicPreviewResponse(
            generation=link.generation,
            alias=link.alias,
            short_url=link.short_url,
            status=status,
            created_at=created_at.isoformat() if created_at else None,
            password_protected=password_protected,
            destination=destination,
            geo_destinations=None,  # geo targeting is v2-only
        )
