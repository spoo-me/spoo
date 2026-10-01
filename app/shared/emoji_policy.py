"""
v2 emoji-alias policy — canonicalization, RGI-subset validation, and
generation-pool derivation. Pure, framework-agnostic, stateless.

Why a policy exists at all: browsers resolve any percent-encoded path, but
many emoji are byte-fragile (the same glyph can be produced with or without
an invisible ``U+FE0F`` variation selector) or don't render on major
platforms (ZWJ sequences split apart, regional-indicator flags show as bare
letters on Chromium/Windows, tag sequences can smuggle invisible data).
The v1 system accepted all of them; this module is the fix.

Accepted graphemes are RGI *fully-qualified* sequences that are either a
single codepoint (which, for single codepoints, implies default emoji
presentation — no ``U+FE0F`` ever needed) or a base + skin-tone modifier.
Everything else — VS16-dependent symbols, ZWJ sequences, keycaps, flags,
tag sequences, standalone components — is rejected by construction.

Legacy resolution stays on ``shared.validators.is_emoji_alias`` (lenient,
no policy); THIS module governs everything newly created. Like the other
``shared`` validators, nothing here reads settings — every knob is a
parameter with a module-level default, enforced by the service layer.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from functools import lru_cache
from typing import Literal
from urllib.parse import unquote

import emoji
import regex

# Acceptance cap for custom aliases: Emoji 15.1 renders on iOS 17.4+/
# Android 14+/Windows 11 23H2+ — two OS generations of headroom.
DEFAULT_ACCEPT_MAX_VERSION: float = 15.1
# Generation cap: Windows 10's Segoe UI Emoji froze at Emoji 12.0 (May 2019
# was its last emoji update); auto-generated codes must render everywhere.
DEFAULT_GENERATE_MAX_VERSION: float = 12.0
DEFAULT_MAX_GRAPHEMES: int = 15

EmojiAliasVerdict = Literal["ok", "empty", "length", "policy"]

_ALNUM_ALIAS_RE = re.compile(r"[a-zA-Z0-9_-]*\Z")
_GRAPHEME_RE = regex.compile(r"\X")
_VS16 = "️"
_SKIN_TONE_MIN = "\U0001f3fb"
_SKIN_TONE_MAX = "\U0001f3ff"
_REGIONAL_INDICATOR_RANGE = (0x1F1E6, 0x1F1FF)

# Committed canonical Unicode grouping (see data/emoji_groups.json and
# scripts/gen_emoji_groups.py). PRESENTATION metadata only: it is joined onto
# the policy-derived accepted set by char to give the picker categories and a
# sensible opening order. It never decides which emoji are accepted.
_GROUP_DATA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "emoji_groups.json",
)
# Display group for an accepted emoji absent from the grouping data. Does not
# occur for the pinned dataset (every accepted char is covered); a guard so a
# future emoji pin outpacing the artifact annotates rather than drops.
FALLBACK_GROUP: str = "Symbols"
# Unknown chars sort last (stable), staying present but out of the way.
_FALLBACK_ORDER: int = 1 << 30


def is_emoji_candidate(alias: str) -> bool:
    """Return True if *alias* contains any character outside ``[A-Za-z0-9_-]``.

    Coarse shape test used to route input onto the emoji validation path.
    NOT a validity check — garbage input is also a "candidate" and must be
    rejected downstream by :func:`is_emoji_only_shape` / :func:`check_emoji_alias`.
    """
    return _ALNUM_ALIAS_RE.fullmatch(alias) is None


def canonicalize_emoji_alias(alias: str) -> str:
    """Return the canonical stored/lookup form of an emoji alias.

    unquote → NFC → strip ``U+FE0F``. Idempotent, and a no-op on already
    canonical input. Routes may have unquoted the path once already; the
    second unquote here is harmless because neither accepted grammar
    (alphanumeric or policy-valid emoji) contains ``%``.

    The accepted emoji subset never *needs* VS16 (single-codepoint
    fully-qualified emoji have default emoji presentation), so stray
    selectors from keyboards/copy-paste are dropped rather than 404ing —
    ``⭐️`` and ``⭐`` resolve to the same link.
    """
    decoded = unicodedata.normalize("NFC", unquote(alias))
    return decoded.replace(_VS16, "")


def is_emoji_only_shape(alias: str) -> bool:
    """Structural gate: every grapheme of *alias* is emoji-shaped.

    A grapheme is emoji-shaped when its canonical form appears in the
    ``emoji`` package's ``EMOJI_DATA`` under *any* qualification status —
    this deliberately includes ZWJ sequences, flags, and keycaps so that
    DTO-level checks can reject *mixed/garbage* input (422) while leaving
    policy decisions (400) to :func:`check_emoji_alias`.
    """
    canonical = canonicalize_emoji_alias(alias)
    if not canonical:
        return False
    return all(g in emoji.EMOJI_DATA for g in _GRAPHEME_RE.findall(canonical))


def check_emoji_alias(
    canonical: str,
    *,
    max_graphemes: int = DEFAULT_MAX_GRAPHEMES,
    max_version: float = DEFAULT_ACCEPT_MAX_VERSION,
) -> EmojiAliasVerdict:
    """Validate an already-canonicalized alias against the emoji policy.

    Callers must pass the output of :func:`canonicalize_emoji_alias`.
    Returns the first failing check: ``"empty"``, ``"length"`` (grapheme
    count above *max_graphemes*), ``"policy"`` (any grapheme outside the
    accepted subset), or ``"ok"``.
    """
    if not canonical:
        return "empty"
    graphemes = _GRAPHEME_RE.findall(canonical)
    if len(graphemes) > max_graphemes:
        return "length"
    if all(_grapheme_accepted(g, max_version) for g in graphemes):
        return "ok"
    return "policy"


def _grapheme_accepted(grapheme: str, max_version: float) -> bool:
    """One grapheme passes policy: RGI fully-qualified, version-capped, and
    single-codepoint or base+skin-tone.

    Single codepoint + fully_qualified implies ``Emoji_Presentation=Yes``
    (text-default symbols like ``☺`` are *unqualified* without VS16), so no
    separate presentation table is needed. The 2-codepoint arm only admits
    skin-tone modifiers — flags (regional-indicator pairs), keycaps, ZWJ and
    tag sequences all fail here or on the status check.
    """
    data = emoji.EMOJI_DATA.get(grapheme)
    if data is None or data["status"] != emoji.STATUS["fully_qualified"]:
        return False
    if data.get("E", float("inf")) > max_version:
        return False
    if len(grapheme) == 1:
        return True
    return len(grapheme) == 2 and _SKIN_TONE_MIN <= grapheme[1] <= _SKIN_TONE_MAX


def emoji_display_name(char: str) -> str:
    """Human-readable name for *char* from the ``emoji`` package's ``en``
    field — the same single source of truth the accepted set is derived
    from, so no second dataset is introduced.

    ``:rocket:`` -> ``"rocket"``: surrounding colons stripped, underscores
    spaced, lowercased. Powers client-side search (resolve "rocket" -> 🚀).
    """
    return emoji.EMOJI_DATA[char]["en"].strip(":").replace("_", " ").lower()


def emoji_keywords(char: str) -> tuple[str, ...]:
    """Extra search aliases for *char* from the ``emoji`` package's ``alias``
    list, cleaned like :func:`emoji_display_name` and with the display name
    itself dropped. Empty when the package lists no aliases for *char*.

    Cleanly available from the pinned package (no second dataset, no
    hand-maintained map), so it rides along to widen name search.
    """
    name = emoji_display_name(char)
    seen: dict[str, None] = {}
    for alias in emoji.EMOJI_DATA[char].get("alias", ()):
        cleaned = alias.strip(":").replace("_", " ").lower()
        if cleaned and cleaned != name:
            seen.setdefault(cleaned, None)
    return tuple(seen)


@lru_cache(maxsize=1)
def _emoji_group_index() -> dict[str, tuple[str, int]]:
    """Load the committed canonical grouping: char -> (group name, order).

    PRESENTATION metadata only (see :data:`_GROUP_DATA_PATH`). Joined onto the
    policy-derived accepted set by char; it never decides membership. Loaded
    and cached once per process — no import-time I/O, no runtime network.
    """
    with open(_GROUP_DATA_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    groups = data["groups"]
    return {
        char: (groups[gi], order) for order, (char, gi) in enumerate(data["entries"])
    }


def emoji_group(char: str) -> str:
    """Canonical Unicode category display name for *char* (e.g. ``"Smileys &
    Emotion"``), for a picker's category tabs.

    Falls back to :data:`FALLBACK_GROUP` for an accepted emoji missing from the
    grouping data, so the entry is annotated rather than dropped.
    """
    entry = _emoji_group_index().get(char)
    return entry[0] if entry else FALLBACK_GROUP


def emoji_sort_key(char: str) -> int:
    """Canonical Unicode ordering position for *char* — group order, then
    within-group order — so a picker opens on Smileys rather than symbols.

    Unknown chars sort last (stably), staying present but out of the way. This
    is presentation ordering only; it does not touch accepted-set membership.
    """
    entry = _emoji_group_index().get(char)
    return entry[1] if entry else _FALLBACK_ORDER


@lru_cache(maxsize=8)
def accepted_singletons(
    max_version: float = DEFAULT_ACCEPT_MAX_VERSION,
) -> tuple[str, ...]:
    """Every single-codepoint grapheme the policy accepts at *max_version*.

    The single-codepoint arm of :func:`_grapheme_accepted`, materialized
    into the exhaustive set a user may CHOOSE from at a given cap. It runs
    the very same predicate the validator applies per grapheme, so
    membership here is exactly ``check_emoji_alias(e) == "ok"`` for any
    one-codepoint ``e`` (below the length cap).

    Base + skin-tone combinations are deliberately NOT enumerated: the base
    emoji is enough for a picker and skin tone is a client-side modifier, so
    the accepted set is single-codepoint only. Derived (not checked in) so
    it can never drift from the validator; ``emoji`` is version-pinned.

    This is the ONE derivation of the accepted space: :func:`generation_pool`
    is this set at the (lower) generation cap, minus regional indicators.
    """
    pool = tuple(
        e
        for e in emoji.EMOJI_DATA
        if len(e) == 1 and _grapheme_accepted(e, max_version)
    )
    if not pool:
        raise ValueError(f"empty accepted emoji set for max_version={max_version}")
    return pool


@lru_cache(maxsize=4)
def generation_pool(
    max_version: float = DEFAULT_GENERATE_MAX_VERSION,
) -> tuple[str, ...]:
    """Derive the auto-generation emoji pool from the accepted set.

    :func:`accepted_singletons` at *max_version* (the generation cap sits
    below the acceptance cap so auto-generated codes render on older
    platforms), minus regional indicators (belt-and-suspenders — single
    indicators are not fully-qualified anyway, so they are already absent).
    Every pool entry therefore passes :func:`check_emoji_alias` by
    construction; unit tests pin that invariant.
    """
    lo, hi = _REGIONAL_INDICATOR_RANGE
    pool = tuple(
        e for e in accepted_singletons(max_version) if not (lo <= ord(e) <= hi)
    )
    if not pool:
        raise ValueError(f"empty emoji generation pool for max_version={max_version}")
    return pool


def vs16_insensitive_pattern(canonical: str) -> str:
    """Anchored regex matching *canonical* with an optional ``U+FE0F`` after
    each codepoint.

    Used for collision checks against the legacy ``emojis`` collection,
    whose ``_id``s may contain historically-accepted variation selectors —
    a legacy ``⭐️🎉`` must block a new canonical ``⭐🎉``.

    The VS16 is wrapped in a group: MongoDB's PCRE matches bytewise, so a
    bare ``\\uFE0F?`` would make only the selector's LAST UTF-8 byte
    optional (silently never matching the selector-free form). Python's
    per-codepoint str ``re`` hides that mistake — pinned by bytes-mode
    regex tests, which reproduce Mongo's bytewise matching.
    """
    return "^" + "".join(re.escape(char) + f"(?:{_VS16})?" for char in canonical) + "$"
