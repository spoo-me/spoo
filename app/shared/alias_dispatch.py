"""Alias-resolution dispatch order — single source of truth.

Which URL generation answers a short code is a public contract shared by
every resolving surface (the redirect via ``UrlService._dispatch`` and
the public preview endpoint). The order lives here as one pure function
so the surfaces can never drift: if the length heuristic ever changes,
both learn about it together — otherwise the preview could describe one
link while the redirect serves another under the same code.

Mirrors the original get_url_by_length_and_type() heuristic:
  emoji alias → urlsV2 first, emojis collection fallback
  6 chars     → urls (v1) first, urlsV2 fallback
  anything else → urlsV2 first, urls (v1) fallback
(7-char codes were historically an explicit branch, but their v2-first
order is identical to the default. Emoji originally resolved against the
emojis collection only; v2 comes first now that emoji aliases are created
as urlsV2 rows — creation-time collision checks against the legacy
collection guarantee a v2 hit can never shadow a live legacy variant.)

Emoji codes look up under two forms, and the split is deliberate:
urlsV2 stores only the canonical form (``shared.emoji_policy``), while
legacy ``emojis`` ``_id``s are whatever bytes v1 accepted — so v2 reads
use :func:`v2_lookup_code` and legacy reads try each of
:func:`emoji_lookup_candidates` (raw first, exact legacy semantics).
"""

from __future__ import annotations

from app.schemas.models.url import SchemaVersion
from app.shared.emoji_policy import canonicalize_emoji_alias
from app.shared.validators import is_emoji_alias

_V1_FIRST_LENGTH = 6  # v1 codes were generated at exactly this length


def is_emoji_shaped(short_code: str) -> bool:
    """Emoji detection for dispatch — canonicalization-aware.

    ``is_emoji_alias`` alone misses byte-variant forms the ``emoji``
    package doesn't index: a redundant ``U+FE0F`` on a default-emoji
    codepoint (``⭐️``) makes the raw string unrecognizable even though
    the canonical form is a plain emoji alias. Those variants are exactly
    what canonicalization exists to rescue, so test both forms.
    """
    if is_emoji_alias(short_code):
        return True
    canonical = canonicalize_emoji_alias(short_code)
    return canonical != short_code and is_emoji_alias(canonical)


def resolution_order(short_code: str) -> tuple[SchemaVersion, ...]:
    """Return the generation lookup order for *short_code*.

    The code must already be percent-decoded (``urllib.parse.unquote``);
    emoji detection tolerates encoded input, but the repositories match
    the decoded form exactly.
    """
    if is_emoji_shaped(short_code):
        return (SchemaVersion.V2, SchemaVersion.EMOJI)
    if len(short_code) == _V1_FIRST_LENGTH:
        return (SchemaVersion.V1, SchemaVersion.V2)
    return (SchemaVersion.V2, SchemaVersion.V1)


def v2_lookup_code(short_code: str) -> str:
    """The alias form used for urlsV2 lookups.

    Canonical for emoji-shaped codes (v2 stores canonical aliases only —
    a pasted ``⭐️`` variant must still find the stored ``⭐`` row);
    unchanged for everything else.
    """
    if is_emoji_shaped(short_code):
        return canonicalize_emoji_alias(short_code)
    return short_code


def emoji_lookup_candidates(short_code: str) -> tuple[str, ...]:
    """``_id`` candidates for the legacy ``emojis`` collection.

    Raw form first — every historically-created alias must stay routable
    by its exact bytes — then the canonical form, so VS16 variants of a
    canonically-stored legacy link resolve instead of 404ing.
    """
    canonical = canonicalize_emoji_alias(short_code)
    if canonical == short_code:
        return (short_code,)
    return (short_code, canonical)
