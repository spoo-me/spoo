"""Unit tests for shared.tags — the one normaliser every tag input runs through."""

from __future__ import annotations

import pytest

from app.shared.tags import (
    TAG_MAX_LENGTH,
    TAGS_MAX_PER_LINK,
    normalise_tag,
    normalise_tags,
)


class TestNormaliseTag:
    def test_trims_casefolds_and_collapses_whitespace(self):
        assert normalise_tag("  Launch   Q3 ") == "launch q3"

    def test_strips_control_characters(self):
        assert normalise_tag("la\x00unch\x1f") == "launch"

    def test_tabs_and_newlines_are_whitespace_not_glue(self):
        assert normalise_tag("launch\tq3") == "launch q3"
        assert normalise_tag("launch\n\nq3") == "launch q3"

    def test_unicode_letters_and_marks_allowed(self):
        assert normalise_tag("Straße") == "strasse"
        assert normalise_tag("हिंदी") == "हिंदी"

    def test_allowed_punctuation(self):
        assert normalise_tag("v1.2-rc_1") == "v1.2-rc_1"

    @pytest.mark.parametrize("bad", ["", "   ", "\x00"])
    def test_empty_rejected(self, bad):
        with pytest.raises(ValueError, match="empty"):
            normalise_tag(bad)

    @pytest.mark.parametrize("bad", ["a,b", "a#b", "a/b", "tag!", "a@b", "x:y"])
    def test_disallowed_characters_rejected(self, bad):
        with pytest.raises(ValueError, match="may only contain"):
            normalise_tag(bad)

    def test_length_cap_is_inclusive(self):
        assert normalise_tag("a" * TAG_MAX_LENGTH) == "a" * TAG_MAX_LENGTH
        with pytest.raises(ValueError, match="exceeds"):
            normalise_tag("a" * (TAG_MAX_LENGTH + 1))

    def test_non_string_rejected(self):
        with pytest.raises(ValueError, match="strings"):
            normalise_tag(3)


class TestNormaliseTags:
    def test_none_is_empty(self):
        assert normalise_tags(None) == []

    def test_dedupes_after_normalisation_first_wins(self):
        assert normalise_tags(["Launch", "launch ", "q3", "LAUNCH"]) == ["launch", "q3"]

    def test_count_cap_is_inclusive(self):
        ok = [str(i) for i in range(TAGS_MAX_PER_LINK)]
        assert normalise_tags(ok) == ok
        with pytest.raises(ValueError, match="at most"):
            normalise_tags([*ok, "one more"])

    def test_duplicates_do_not_count_toward_cap(self):
        assert normalise_tags(["a"] * (TAGS_MAX_PER_LINK * 2)) == ["a"]

    def test_cap_none_lifts_the_limit(self):
        many = [str(i) for i in range(TAGS_MAX_PER_LINK + 5)]
        assert normalise_tags(many, cap=None) == many

    def test_non_list_passes_through_for_pydantic(self):
        assert normalise_tags("launch") == "launch"

    def test_one_bad_item_rejects_the_list(self):
        with pytest.raises(ValueError):
            normalise_tags(["fine", "not,fine"])
