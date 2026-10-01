"""
Response DTO for GET /api/v1/emoji-set.

A static, build-time-derived catalogue of the emoji-alias acceptance
policy so clients (e.g. an emoji picker) have one source of truth instead
of replicating the rules. Each accepted emoji carries its name so a picker
can offer search by name rather than making users scroll a thousand-plus
glyphs. DTO serializes only — the derivation lives in
``shared.emoji_policy`` and is assembled in the route.
"""

from __future__ import annotations

from pydantic import Field

from app.schemas.dto.base import ResponseBase


class EmojiEntry(ResponseBase):
    """One accepted emoji, enriched for client-side search.

    Names and aliases come from the same pinned ``emoji`` package the set
    itself is derived from, so there is no second dataset to keep in sync.
    """

    c: str = Field(
        description="Raw canonical emoji character (no U+FE0F variation "
        "selector), matching how aliases are stored and echoed."
    )
    n: str = Field(
        description="Human-readable name, lowercased with spaces (e.g. "
        '"rocket"). The primary search key.'
    )
    g: str = Field(
        description="Canonical Unicode category display name (e.g. "
        '"Smileys & Emotion"), for the picker\'s category tabs. Entries in '
        "the array are already sorted by canonical group and within-group "
        "order, so a picker opens on Smileys rather than symbols."
    )
    gen: bool = Field(
        description="Whether this emoji is in the auto-generation pool. "
        "Filter gen=true for the subset the server auto-generates."
    )
    k: list[str] | None = Field(
        default=None,
        description="Extra search aliases when the source lists any (e.g. "
        '"tada" for the party popper); omitted otherwise. Name search is '
        "the floor; these only widen it.",
    )


class EmojiSetResponse(ResponseBase):
    """The accepted emoji catalogue and its policy caps.

    Emoji values are RAW characters in canonical form (no ``U+FE0F``). Each
    carries its canonical Unicode category (``g``) and the array is ordered by
    canonical group and within-group order, so a picker can render category
    tabs and open on Smileys rather than symbols.
    """

    accept_max_version: float = Field(
        description="Newest Unicode emoji version a custom alias may use."
    )
    generate_max_version: float = Field(
        description="Cap for auto-generated emoji aliases (lower, for older "
        "platform coverage)."
    )
    max_graphemes: int = Field(
        description="Maximum number of emoji graphemes allowed in one alias."
    )
    emoji: list[EmojiEntry] = Field(
        description="Every single-codepoint emoji a user may choose, at the "
        "acceptance cap, each with its name and whether it is in the "
        "generation pool. This is the picker's list. Skin-tone variants are "
        "NOT enumerated: the base emoji suffices and skin tone is a "
        "client-side modifier appended to the base."
    )
