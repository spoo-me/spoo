from __future__ import annotations

import re
from urllib.parse import quote

import emoji as _emoji
import pytest
import regex

from app.shared.emoji_policy import (
    DEFAULT_ACCEPT_MAX_VERSION,
    DEFAULT_GENERATE_MAX_VERSION,
    FALLBACK_GROUP,
    accepted_singletons,
    canonicalize_emoji_alias,
    check_emoji_alias,
    emoji_display_name,
    emoji_group,
    emoji_keywords,
    emoji_sort_key,
    generation_pool,
    is_emoji_candidate,
    is_emoji_only_shape,
    vs16_insensitive_pattern,
)

VS16 = "️"

STAR = "⭐"  # ⭐ single cp, fully-qualified, E0.6
PARTY = "\U0001f389"  # 🎉 single cp, E0.6
GRINNING = "\U0001f600"  # 😀 first entry in canonical order (Smileys & Emotion)
RED_CIRCLE = "\U0001f534"  # 🔴 accepted, in the Symbols group (sorts far after)
THUMBS_MEDIUM = "\U0001f44d\U0001f3fd"  # 👍🏽 base + skin tone, E1.0
SMILEY_TEXT_DEFAULT = "☺"  # ☺ unqualified without VS16
WOMAN_TECHNOLOGIST = "\U0001f469‍\U0001f4bb"  # 👩‍💻 ZWJ sequence
RAINBOW_FLAG = "\U0001f3f3️‍\U0001f308"  # 🏳️‍🌈 ZWJ sequence
KEYCAP_ONE = "1️⃣"  # 1️⃣
US_FLAG = "\U0001f1fa\U0001f1f8"  # 🇺🇸 regional indicators
ENGLAND_FLAG = "\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f"  # 🏴󠁧󠁢󠁥󠁮󠁧󠁿 tag sequence
SKIN_SWATCH = "\U0001f3ff"  # 🏿 standalone component
MELTING_FACE = "\U0001fae0"  # 🫠 E14.0
SHAKING_FACE = "\U0001fae8"  # 🫨 E15.0


class TestIsEmojiCandidate:
    @pytest.mark.parametrize("alias", ["mylink", "a-b_c9", "", "ABC123"])
    def test_alnum_is_not_candidate(self, alias):
        assert is_emoji_candidate(alias) is False

    @pytest.mark.parametrize("alias", [STAR, PARTY * 3, "abc" + PARTY, "café", "%F0"])
    def test_anything_else_is_candidate(self, alias):
        assert is_emoji_candidate(alias) is True


class TestCanonicalizeEmojiAlias:
    def test_strips_vs16(self):
        assert canonicalize_emoji_alias(STAR + VS16 + PARTY) == STAR + PARTY

    def test_unquotes_percent_encoding(self):
        assert canonicalize_emoji_alias(quote(STAR + PARTY)) == STAR + PARTY

    def test_unquotes_then_strips(self):
        assert canonicalize_emoji_alias(quote(STAR + VS16)) == STAR

    def test_nfc_normalizes(self):
        # e + combining acute → é (non-emoji input is normalized, not mangled)
        assert canonicalize_emoji_alias("é") == "é"

    def test_idempotent(self):
        once = canonicalize_emoji_alias(STAR + VS16 + THUMBS_MEDIUM)
        assert canonicalize_emoji_alias(once) == once

    def test_noop_on_alnum(self):
        assert canonicalize_emoji_alias("mylink") == "mylink"


class TestIsEmojiOnlyShape:
    @pytest.mark.parametrize(
        "alias",
        [
            STAR,
            PARTY * 3,
            THUMBS_MEDIUM,
            # Policy-rejected but emoji-SHAPED — must pass the structural
            # gate so they reach the service and fail as policy (400),
            # not as a DTO 422.
            WOMAN_TECHNOLOGIST,
            US_FLAG,
            KEYCAP_ONE,
            ENGLAND_FLAG,
            SMILEY_TEXT_DEFAULT + VS16,
        ],
    )
    def test_emoji_shaped(self, alias):
        assert is_emoji_only_shape(alias) is True

    @pytest.mark.parametrize(
        "alias",
        ["abc", "abc" + PARTY, PARTY + "x", "", " ", STAR + " " + STAR, "café"],
    )
    def test_mixed_or_garbage_rejected(self, alias):
        assert is_emoji_only_shape(alias) is False


class TestCheckEmojiAlias:
    @pytest.mark.parametrize(
        "alias",
        [STAR, PARTY, THUMBS_MEDIUM, STAR + PARTY + THUMBS_MEDIUM, PARTY * 15],
    )
    def test_accepted(self, alias):
        assert check_emoji_alias(alias) == "ok"

    def test_empty(self):
        assert check_emoji_alias("") == "empty"

    def test_too_many_graphemes(self):
        assert check_emoji_alias(PARTY * 16) == "length"

    def test_grapheme_cap_configurable(self):
        assert check_emoji_alias(PARTY * 3, max_graphemes=2) == "length"

    @pytest.mark.parametrize(
        "alias",
        [
            SMILEY_TEXT_DEFAULT,  # unqualified without VS16 (byte-fragile)
            WOMAN_TECHNOLOGIST,  # ZWJ sequence
            canonicalize_emoji_alias(RAINBOW_FLAG),  # ZWJ sequence
            canonicalize_emoji_alias(KEYCAP_ONE),  # keycap
            US_FLAG,  # regional-indicator flag
            ENGLAND_FLAG,  # tag sequence
            SKIN_SWATCH,  # standalone component
            "abc",  # not emoji at all
        ],
    )
    def test_policy_rejected(self, alias):
        assert check_emoji_alias(alias) == "policy"

    def test_version_cap(self):
        assert check_emoji_alias(SHAKING_FACE, max_version=12.0) == "policy"
        assert check_emoji_alias(SHAKING_FACE, max_version=15.1) == "ok"
        assert check_emoji_alias(MELTING_FACE, max_version=12.0) == "policy"
        assert (
            check_emoji_alias(MELTING_FACE, max_version=DEFAULT_ACCEPT_MAX_VERSION)
            == "ok"
        )

    def test_expects_canonical_input(self):
        # Raw VS16 forms are the caller's job to canonicalize first;
        # uncanonicalized input fails closed rather than resolving.
        assert check_emoji_alias(STAR + VS16) == "policy"


class TestGenerationPool:
    def test_pool_non_empty_and_stable(self):
        pool = generation_pool()
        assert len(pool) > 500
        assert pool is generation_pool()  # lru_cache

    def test_all_single_codepoint(self):
        assert all(len(e) == 1 for e in generation_pool())

    def test_every_entry_passes_acceptance_policy(self):
        # THE invariant: the generator can never emit something the
        # validator rejects. Turns any future `emoji` package bump that
        # breaks this into an explicit test failure.
        for e in generation_pool():
            assert check_emoji_alias(e) == "ok", f"pool entry rejected: {e!r}"

    def test_version_cap_respected(self):
        for e in generation_pool():
            assert _emoji.EMOJI_DATA[e]["E"] <= DEFAULT_GENERATE_MAX_VERSION

    def test_no_regional_indicators(self):
        assert not any(0x1F1E6 <= ord(e) <= 0x1F1FF for e in generation_pool())

    def test_wider_cap_is_superset(self):
        narrow, wide = set(generation_pool(12.0)), set(generation_pool(15.1))
        assert narrow < wide

    def test_empty_pool_raises(self):
        with pytest.raises(ValueError):
            generation_pool(0.1)


class TestAcceptedSingletons:
    def test_non_empty_and_cached(self):
        accepted = accepted_singletons()
        assert len(accepted) > 500
        assert accepted is accepted_singletons()  # lru_cache

    def test_all_single_codepoint(self):
        assert all(len(e) == 1 for e in accepted_singletons())

    def test_agrees_with_validator(self):
        # THE contract: membership is exactly what check_emoji_alias accepts
        # for a single-codepoint grapheme, so the picker can never offer
        # something the create endpoint would 400.
        for e in accepted_singletons():
            assert check_emoji_alias(e) == "ok", f"accepted entry rejected: {e!r}"

    def test_contains_known_safe_emoji(self):
        accepted = set(accepted_singletons())
        assert STAR in accepted
        assert PARTY in accepted

    def test_excludes_policy_rejected_forms(self):
        accepted = set(accepted_singletons())
        assert SMILEY_TEXT_DEFAULT not in accepted  # text-default (needs VS16)
        assert SKIN_SWATCH not in accepted  # standalone component
        # Multi-codepoint forms are excluded by construction (single-cp only).
        assert THUMBS_MEDIUM not in accepted  # base + skin tone
        assert US_FLAG not in accepted  # regional-indicator pair
        assert WOMAN_TECHNOLOGIST not in accepted  # ZWJ sequence

    def test_accept_cap_is_superset_of_generate_cap(self):
        narrow = set(accepted_singletons(DEFAULT_GENERATE_MAX_VERSION))
        wide = set(accepted_singletons(DEFAULT_ACCEPT_MAX_VERSION))
        assert narrow < wide
        # Newer emoji live only in the wider (acceptance) cap.
        assert MELTING_FACE in wide and MELTING_FACE not in narrow

    def test_generation_pool_derives_from_accepted(self):
        # generation_pool is accepted_singletons at the generate cap minus
        # regional indicators (a no-op, since single indicators aren't
        # fully-qualified) — so the pool is a subset of the accepted set.
        assert set(generation_pool()) <= set(accepted_singletons())
        assert set(generation_pool(DEFAULT_GENERATE_MAX_VERSION)) <= set(
            accepted_singletons(DEFAULT_GENERATE_MAX_VERSION)
        )

    def test_empty_set_raises(self):
        with pytest.raises(ValueError):
            accepted_singletons(0.1)


class TestEmojiDisplayName:
    def test_strips_colons_spaces_underscores_lowercases(self):
        assert emoji_display_name("\U0001f680") == "rocket"
        assert emoji_display_name(PARTY) == "party popper"

    def test_names_non_empty_and_colon_free_for_accepted(self):
        for e in accepted_singletons():
            name = emoji_display_name(e)
            assert name
            assert ":" not in name
            assert name == name.lower()


class TestEmojiKeywords:
    def test_extra_aliases_cleaned(self):
        # 🎉 has alias ":tada:" beyond its "party popper" name.
        assert "tada" in emoji_keywords(PARTY)

    def test_excludes_the_display_name(self):
        for e in accepted_singletons():
            assert emoji_display_name(e) not in emoji_keywords(e)

    def test_empty_when_no_aliases(self):
        # ⭐ / 🚀 carry no alias list in the pinned package.
        assert emoji_keywords(STAR) == ()
        assert emoji_keywords("\U0001f680") == ()


CANONICAL_GROUPS = {
    "Smileys & Emotion",
    "People & Body",
    "Component",
    "Animals & Nature",
    "Food & Drink",
    "Travel & Places",
    "Activities",
    "Objects",
    "Symbols",
    "Flags",
}


class TestEmojiGrouping:
    def test_known_chars_map_to_expected_group(self):
        assert emoji_group(GRINNING) == "Smileys & Emotion"
        assert emoji_group("\U0001f680") == "Travel & Places"  # 🚀 rocket
        assert emoji_group(RED_CIRCLE) == "Symbols"

    def test_every_accepted_char_has_a_canonical_group(self):
        # PRESENTATION join is total over the accepted set: every entry gets a
        # real canonical group (the pinned dataset has full coverage), never
        # the fallback, and never a group outside the canonical ten.
        for e in accepted_singletons():
            assert emoji_group(e) in CANONICAL_GROUPS

    def test_unknown_char_falls_back_and_sorts_last(self):
        assert emoji_group("A") == FALLBACK_GROUP
        assert emoji_group("A") in CANONICAL_GROUPS
        assert emoji_sort_key("A") > emoji_sort_key(RED_CIRCLE)

    def test_sort_key_opens_on_smileys_not_symbols(self):
        # A smiley sorts before a symbol, so a picker opens on Smileys.
        assert emoji_sort_key(GRINNING) < emoji_sort_key(RED_CIRCLE)
        assert emoji_sort_key(GRINNING) == 0  # 😀 is first in canonical order

    def test_first_accepted_in_canonical_order_is_a_smiley(self):
        first = min(accepted_singletons(), key=emoji_sort_key)
        assert emoji_group(first) == "Smileys & Emotion"

    def test_grouping_does_not_change_accepted_set(self):
        # THE invariant: sorting by the presentation key is a pure permutation
        # of the policy-derived accepted set — same members, same count.
        accepted = accepted_singletons()
        ordered = sorted(accepted, key=emoji_sort_key)
        assert len(ordered) == len(accepted)
        assert set(ordered) == set(accepted)


class TestVs16InsensitivePattern:
    def test_matches_canonical_and_vs16_variants(self):
        pattern = re.compile(vs16_insensitive_pattern(STAR + PARTY))
        assert pattern.fullmatch(STAR + PARTY)
        assert pattern.fullmatch(STAR + VS16 + PARTY)
        assert pattern.fullmatch(STAR + PARTY + VS16)
        assert pattern.fullmatch(STAR + VS16 + PARTY + VS16)

    def test_rejects_other_aliases(self):
        pattern = re.compile(vs16_insensitive_pattern(STAR + PARTY))
        assert pattern.fullmatch(STAR) is None
        assert pattern.fullmatch(PARTY + STAR) is None
        assert pattern.fullmatch(STAR + PARTY + PARTY) is None

    def test_escapes_regex_metacharacters(self):
        # Defense in depth: canonical input should never contain these,
        # but the pattern must stay literal if it ever does.
        pattern = re.compile(vs16_insensitive_pattern("a.b"))
        assert pattern.fullmatch("a.b")
        assert pattern.fullmatch("axb") is None

    def test_matches_under_bytewise_semantics(self):
        # THE regression pin for the Mongo bug: MongoDB's PCRE matches
        # bytewise, so an ungrouped `️?` makes only the selector's
        # LAST UTF-8 byte optional and never matches the selector-free
        # form. Python's per-codepoint str matching hides that — only
        # bytes-mode re reproduces Mongo's behavior, so the pattern must
        # pass here too.
        pattern = re.compile(vs16_insensitive_pattern(STAR + PARTY).encode())
        assert pattern.fullmatch((STAR + PARTY).encode())
        assert pattern.fullmatch((STAR + VS16 + PARTY).encode())
        assert pattern.fullmatch((STAR + VS16 + PARTY + VS16).encode())
        assert pattern.fullmatch((PARTY + STAR).encode()) is None


class TestGraphemeSegmentation:
    """Pin the \\X behavior the policy depends on."""

    @pytest.mark.parametrize(
        ("s", "count"),
        [
            (THUMBS_MEDIUM, 1),
            (US_FLAG, 1),
            (US_FLAG * 2, 2),
            (WOMAN_TECHNOLOGIST, 1),
            (ENGLAND_FLAG, 1),
            (STAR + PARTY, 2),
        ],
    )
    def test_grapheme_counts(self, s, count):
        assert len(regex.findall(r"\X", s)) == count
