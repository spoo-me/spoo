"""Unit tests for shared/alias_dispatch.py — the resolution-order table.

This order is a public contract shared by the redirect and the public
preview endpoint; the table below is the single place it is spelled out.
"""

from __future__ import annotations

import pytest

from app.schemas.models.url import SchemaVersion
from app.shared.alias_dispatch import (
    emoji_lookup_candidates,
    is_emoji_shaped,
    resolution_order,
    v2_lookup_code,
)

VS16 = "️"
STAR = "⭐"
ROCKET = "🚀"


@pytest.mark.parametrize(
    ("short_code", "expected"),
    [
        # emoji aliases → urlsV2 first (canonical), emojis collection fallback
        ("🚀", (SchemaVersion.V2, SchemaVersion.EMOJI)),
        ("🚀✨", (SchemaVersion.V2, SchemaVersion.EMOJI)),
        # percent-encoded emoji still detected (is_emoji_alias unquotes)
        ("%F0%9F%9A%80", (SchemaVersion.V2, SchemaVersion.EMOJI)),
        # redundant-VS16 variant (⭐️): invisible to is_emoji_alias on the
        # raw bytes, detected via its canonical form
        ("⭐️🚀", (SchemaVersion.V2, SchemaVersion.EMOJI)),
        # 6 chars → v1 first (v1 codes were generated at this length)
        ("abc123", (SchemaVersion.V1, SchemaVersion.V2)),
        ("Docs12", (SchemaVersion.V1, SchemaVersion.V2)),
        # 7 chars → v2 first (historically an explicit branch, same order
        # as the default)
        ("abcd123", (SchemaVersion.V2, SchemaVersion.V1)),
        # anything else → v2 first
        ("ab", (SchemaVersion.V2, SchemaVersion.V1)),
        ("short", (SchemaVersion.V2, SchemaVersion.V1)),
        ("a-much-longer-alias", (SchemaVersion.V2, SchemaVersion.V1)),
        ("", (SchemaVersion.V2, SchemaVersion.V1)),
    ],
)
def test_resolution_order_table(short_code, expected):
    assert resolution_order(short_code) == expected


class TestIsEmojiShaped:
    @pytest.mark.parametrize(
        "code",
        [
            ROCKET,
            STAR + ROCKET,
            STAR + VS16 + ROCKET,  # redundant VS16, canonical form rescues it
            "☺" + VS16,  # fully-qualified VS16 sequence, raw form matches
            "%F0%9F%9A%80",
        ],
    )
    def test_emoji_shaped(self, code):
        assert is_emoji_shaped(code) is True

    @pytest.mark.parametrize("code", ["abc123", "", "café", "abc" + ROCKET])
    def test_not_emoji_shaped(self, code):
        assert is_emoji_shaped(code) is False


class TestV2LookupCode:
    def test_emoji_code_canonicalized(self):
        assert v2_lookup_code(STAR + VS16 + ROCKET) == STAR + ROCKET

    def test_percent_encoded_emoji_decoded(self):
        assert v2_lookup_code("%F0%9F%9A%80") == ROCKET

    def test_non_emoji_untouched(self):
        # Alphanumeric codes pass through byte-identical — even ones that
        # would decode differently (%41 stays literal for non-emoji).
        assert v2_lookup_code("abc123") == "abc123"
        assert v2_lookup_code("café") == "café"


class TestEmojiLookupCandidates:
    def test_canonical_input_single_candidate(self):
        assert emoji_lookup_candidates(STAR + ROCKET) == (STAR + ROCKET,)

    def test_variant_input_raw_first_then_canonical(self):
        raw = STAR + VS16 + ROCKET
        assert emoji_lookup_candidates(raw) == (raw, STAR + ROCKET)
