"""Weighted destination pick for A/B split links."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from app.schemas.models.url import AbVariant


def pick_variant(variants: Sequence[AbVariant], roll: int) -> int | None:
    """Map a roll in 0..99 onto the variant whose weight band it falls in.

    Bands are laid out in list order; the unclaimed remainder above the
    summed weights maps to None, meaning the default destination.
    """
    ceiling = 0
    for index, variant in enumerate(variants):
        ceiling += variant.weight
        if roll < ceiling:
            return index
    return None


def variant_urls(variants: Iterable[Any] | None) -> list[str]:
    """Destination URLs of ab_variants entries, as models or raw Mongo dicts."""
    return [v["url"] if isinstance(v, dict) else v.url for v in variants or []]
