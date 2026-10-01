"""Edge cache wire contract — KV key format and entry shape.

Consumed by the TS Worker in ``edge/spoo-edge-cache/``; pinned from both
sides by ``edge/spoo-edge-cache/contract/`` (schema + fixtures). Changes
here are cross-language: update the fixtures and the Worker in the same
commit. Promotion policy lives in :mod:`services.edge_cache.promotion`
and can change freely; this module should stay stable.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


def cache_key(domain: str, short_code: str) -> str:
    """``cache:{domain}:{code}`` — the Worker derives the same key from
    the request Host (lowercased, ``www.`` stripped) and path."""
    return f"cache:{domain}:{short_code}"


class EdgeCacheEntry(BaseModel):
    """The KV value the Worker serves from. Schema pinned by
    ``edge/spoo-edge-cache/contract/entry.schema.json``.

    ``redirect``: serve Location to everyone (+ ``og_html`` to preview
    bots when present). ``og_only``: serve ``og_html`` to preview bots,
    everyone else passes through to origin — this keeps click tracking
    for non-hot og-links while bots are answered at the edge.
    """

    model_config = ConfigDict(frozen=True)

    type: Literal["redirect", "og_only"] = "redirect"
    url: str | None = None  # required for redirect, absent for og_only
    status: int = 302
    # Prerendered meta_preview.html (final HTML string, no template syntax).
    # Workers that predate this field ignore it and serve the redirect.
    og_html: str | None = None

    def to_kv_json(self) -> str:
        """Wire format: absent optionals are omitted, not null — pinned by
        the contract fixtures (a plain redirect stays {type,url,status})."""
        return self.model_dump_json(exclude_none=True)


class EdgeCacheGeoEntry(BaseModel):
    """Geo-targeted variant: ``url`` is the default destination and
    ``rules`` maps ISO 3166-1 alpha-2 codes to per-country overrides.
    The Worker picks ``rules[request.cf.country] ?? url`` — the same
    decision origin makes from CF-IPCountry, from the same CF geodata,
    so edge-served and origin-served answers can never disagree.

    Workers that predate this type see an unknown ``type`` and pass
    through to origin (pinned by the malformed-fixtures suite), so the
    entry can ship before or after the Worker deploy."""

    model_config = ConfigDict(frozen=True)

    type: Literal["geo_redirect"] = "geo_redirect"
    url: str
    status: int = 302
    rules: dict[str, str]
    # Same semantics as EdgeCacheEntry.og_html: preview bots get the card,
    # everyone else the geo redirect. Keeps hot geo+meta links correct.
    og_html: str | None = None

    def to_kv_json(self) -> str:
        """Wire format: absent optionals are omitted, not null."""
        return self.model_dump_json(exclude_none=True)
