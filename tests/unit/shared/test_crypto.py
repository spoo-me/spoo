from __future__ import annotations

import hashlib

import pytest

from app.infrastructure.crypto import hash_password, hash_token, verify_password


class TestHashPassword:
    def test_returns_string(self):
        assert isinstance(hash_password("secret"), str)

    def test_differs_from_input(self):
        assert hash_password("secret") != "secret"

    def test_unique_salts(self):
        # argon2 produces a new salt each call
        assert hash_password("same") != hash_password("same")


class TestVerifyPassword:
    @pytest.mark.parametrize(
        "candidate, expected",
        [("correct_password", True), ("wrong_password", False)],
        ids=["correct", "wrong"],
    )
    def test_verify(self, candidate, expected):
        h = hash_password("correct_password")
        assert verify_password(candidate, h) is expected

    def test_invalid_hash_returns_false(self):
        assert verify_password("any", "not-a-valid-hash") is False


@pytest.mark.parametrize(
    "token",
    ["abc", "test", "some_token", "token_with_unicode_🔑"],
    ids=["short", "simple", "with_underscore", "unicode"],
)
def test_hash_token_is_hex64(token):
    h = hash_token(token)
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_hash_token_deterministic():
    assert hash_token("abc") == hash_token("abc")


def test_hash_token_known_value():
    assert hash_token("test") == hashlib.sha256(b"test").hexdigest()


def test_hash_token_distinct_inputs():
    assert hash_token("token_a") != hash_token("token_b")


class TestPkceS256Challenge:
    def test_rfc7636_appendix_b_vector(self):
        from app.infrastructure.crypto import pkce_s256_challenge

        assert (
            pkce_s256_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
            == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        )

    def test_challenge_is_43_chars_unpadded_base64url(self):
        from app.infrastructure.crypto import pkce_s256_challenge

        challenge = pkce_s256_challenge("a" * 43)
        assert len(challenge) == 43
        assert "=" not in challenge
        assert "+" not in challenge
        assert "/" not in challenge

    def test_different_verifiers_differ(self):
        from app.infrastructure.crypto import pkce_s256_challenge

        assert pkce_s256_challenge("a" * 43) != pkce_s256_challenge("b" * 43)
