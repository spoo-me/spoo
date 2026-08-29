from __future__ import annotations

import pytest

from shared.validators import (
    validate_alias,
    validate_blocked_url,
    validate_url,
    validate_url_password,
)

# ---------------------------------------------------------------------------
# shared.validators — validate_url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://example.com", True),
        ("https://example.com/foo/bar?q=1", True),
        ("https://spoo.me/abc", False),
        ("https://SPOO.ME/abc", False),  # case-insensitive block
        ("not-a-url", False),
        ("http://192.168.1.1/path", False),  # IPv4 skipped by validator
    ],
    ids=[
        "valid",
        "valid_with_path",
        "self_ref",
        "self_ref_uppercase",
        "plain_text",
        "ipv4",
    ],
)
def test_validate_url(url, expected):
    assert validate_url(url) is expected


def test_validate_url_custom_blocked_domain():
    assert validate_url("https://evil.com", blocked_self_domains=("evil.com",)) is False


def test_validate_url_empty_blocked_list_allows_spoo():
    assert validate_url("https://spoo.me/x", blocked_self_domains=()) is True


def test_validate_url_malformed_bracket_host_is_invalid_not_an_error():
    # urlparse raises on "]" in the netloc; must answer False, never a 500.
    assert validate_url("https://example.com]:80/x") is False


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com",
        "file:///etc/passwd",
        "data:text/html,foo",
        "javascript:alert(1)",
        "//example.com/path",
        "example.com",
    ],
    ids=["ftp", "file", "data", "javascript", "scheme_relative", "no_scheme"],
)
def test_validate_url_rejects_non_http_schemes(url):
    assert validate_url(url) is False


def test_validate_url_blocks_self_link_across_schemes():
    # Bare hostname catches both http:// and https:// variants — guards
    # against the wiring bug where the full app_url only matched one scheme.
    assert validate_url("http://spoo.me/abc") is False
    assert validate_url("https://spoo.me/abc") is False
    assert validate_url("https://docs.spoo.me/x") is False


# ---------------------------------------------------------------------------
# shared.validators — validate_url_password
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "password, valid",
    [
        ("Hello123.", True),
        ("Hello123@", True),
        ("Hi1.", False),  # too short
        ("12345678.", False),  # no letter
        ("HelloWorld.", False),  # no digit
        ("Hello1234", False),  # no special char
        ("Hello12..", False),  # consecutive specials (..)
        ("Hello12@.", False),  # consecutive specials (@.)
    ],
    ids=[
        "dot_ok",
        "at_ok",
        "too_short",
        "no_letter",
        "no_digit",
        "no_special",
        "consecutive_dots",
        "consecutive_at_dot",
    ],
)
def test_validate_url_password(password, valid):
    assert validate_url_password(password) is valid


# ---------------------------------------------------------------------------
# shared.validators — validate_alias
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "alias, expected",
    [
        ("MyAlias123", True),
        ("my_alias", True),
        ("my-alias", True),
        ("", True),  # zero chars matches *
        ("my alias", False),  # space
        ("alias!", False),  # special char
    ],
    ids=["alphanumeric", "underscore", "hyphen", "empty", "space", "exclamation"],
)
def test_validate_alias(alias, expected):
    assert validate_alias(alias) is expected


# ---------------------------------------------------------------------------
# shared.validators — validate_blocked_url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, patterns, expected",
    [
        ("https://evil.com", [], True),  # no patterns → always allow
        ("https://phishing.example.com", [r"phishing"], False),  # match → block
        ("https://good.example.com", [r"phishing"], True),  # no match → allow
        ("https://spam.ru/page", [r"\.ru/"], False),  # regex applied
        ("https://evil.com", [r"safe", r"evil"], False),  # second pattern matches
    ],
    ids=[
        "no_patterns",
        "match_blocks",
        "no_match_allows",
        "regex_match",
        "second_pattern",
    ],
)
def test_validate_blocked_url(url, patterns, expected):
    assert validate_blocked_url(url, patterns) is expected


def test_validate_blocked_url_timeout_fails_open(mocker):
    """Timed-out patterns must fail open (URL stays allowed)."""
    mocker.patch("shared.validators.regex.search", side_effect=TimeoutError)
    assert validate_blocked_url("https://example.com", [r"any_pattern"]) is True
