from __future__ import annotations

import re
import string

import emoji as _emoji
import pytest

from app.shared.emoji_policy import check_emoji_alias, generation_pool
from app.shared.generators import (
    generate_emoji_alias,
    generate_emoji_alias_v2,
    generate_otp_code,
    generate_secure_token,
    generate_short_code,
    generate_short_code_v2,
)

_ALPHANUM = set(string.ascii_letters + string.digits)


class TestGenerateShortCode:
    def test_length_is_6(self):
        assert len(generate_short_code()) == 6

    def test_only_alphanumeric(self):
        assert set(generate_short_code()).issubset(_ALPHANUM)

    def test_produces_variety(self):
        assert len({generate_short_code() for _ in range(20)}) > 1


class TestGenerateShortCodeV2:
    def test_default_length_is_7(self):
        assert len(generate_short_code_v2()) == 7

    @pytest.mark.parametrize("length", [4, 8, 12, 20])
    def test_custom_length(self, length):
        assert len(generate_short_code_v2(length=length)) == length

    def test_only_alphanumeric(self):
        assert set(generate_short_code_v2()).issubset(_ALPHANUM)


class TestGenerateEmojiAliasV2:
    def test_default_length_is_3(self):
        assert len(_emoji.emoji_list(generate_emoji_alias_v2())) == 3

    @pytest.mark.parametrize("length", [1, 5, 15])
    def test_custom_length(self, length):
        assert len(_emoji.emoji_list(generate_emoji_alias_v2(length))) == length

    @pytest.mark.parametrize("length", [0, 16])
    def test_length_out_of_range(self, length):
        with pytest.raises(ValueError):
            generate_emoji_alias_v2(length)

    def test_draws_from_safe_pool(self):
        pool = set(generation_pool())
        for _ in range(20):
            alias = generate_emoji_alias_v2()
            assert all(ch in pool for ch in alias)

    def test_output_passes_acceptance_policy(self):
        for _ in range(20):
            assert check_emoji_alias(generate_emoji_alias_v2()) == "ok"

    def test_produces_variety(self):
        assert len({generate_emoji_alias_v2() for _ in range(20)}) > 1


class TestGenerateEmojiAlias:
    def test_returns_exactly_3_emojis(self):
        assert len(_emoji.emoji_list(generate_emoji_alias())) == 3

    def test_draws_from_safe_pool(self):
        pool = set(generation_pool())
        assert all(ch in pool for ch in generate_emoji_alias())

    def test_produces_variety(self):
        assert len({generate_emoji_alias() for _ in range(20)}) > 1


class TestGenerateOtpCode:
    @pytest.mark.parametrize("length", [4, 6, 8])
    def test_length(self, length):
        assert len(generate_otp_code(length=length)) == length

    def test_only_digits(self):
        assert generate_otp_code().isdigit()


class TestGenerateSecureToken:
    def test_url_safe_characters(self):
        assert re.match(r"^[A-Za-z0-9_\-]+$", generate_secure_token())

    def test_produces_variety(self):
        assert len({generate_secure_token() for _ in range(10)}) > 1
