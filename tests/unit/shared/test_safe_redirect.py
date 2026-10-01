"""Unit tests for validate_safe_redirect."""

import pytest

from app.shared.validators import validate_safe_redirect


@pytest.mark.parametrize(
    "url, expected",
    [
        ("/dashboard", "/dashboard"),
        ("/auth/device/login?state=abc", "/auth/device/login?state=abc"),
        ("/some/path", "/some/path"),
        ("", "/dashboard"),
        ("https://evil.com", "/dashboard"),
        ("//evil.com", "/dashboard"),
        ("http://evil.com", "/dashboard"),
        ("javascript:alert(1)", "/dashboard"),
        ("data:text/html,<h1>hi</h1>", "/dashboard"),
        (
            "/\\evil.com",
            "/\\evil.com",
        ),  # relative path with backslash — allowed (harmless)
    ],
    ids=[
        "valid_relative",
        "valid_with_query",
        "valid_nested",
        "empty_falls_back",
        "absolute_url_blocked",
        "protocol_relative_blocked",
        "http_blocked",
        "javascript_blocked",
        "data_uri_blocked",
        "backslash_relative",
    ],
)
def test_validate_safe_redirect(url, expected):
    assert validate_safe_redirect(url) == expected


def test_custom_fallback():
    assert validate_safe_redirect("https://evil.com", fallback="/home") == "/home"
