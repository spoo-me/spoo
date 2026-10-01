"""Unit tests for redact_sensitive_fields — secrets scrubbed, analytics fields kept."""

from __future__ import annotations

import pytest

from app.infrastructure.logging import redact_sensitive_fields

REDACTED = "***REDACTED***"


def _redact(event_dict: dict) -> dict:
    return redact_sensitive_fields(None, "info", dict(event_dict))


@pytest.mark.parametrize(
    "field",
    [
        "password",
        "password_hash",
        "token",
        "api_key",
        "authorization",
        "cookie",
        "refresh_token",
        "access_token",
        "secret",
        "key",
        # substring heuristic
        "jwt_secret",
        "client_secret",
        "device_token",
        "raw_password",
        "claim_token",  # claim-links bearer deed must never reach logs
    ],
)
def test_secret_fields_redacted(field: str):
    assert _redact({field: "s3cr3t"})[field] == REDACTED


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("has_password", True),
        ("password_protected", False),
        ("key_id", "6a065366720e95786b0608fb"),
        ("key_prefix", "abcd1234"),
        ("token_prefix", "abcd1234"),
        ("query_keys", ["password", "alias"]),
    ],
)
def test_safe_fields_pass_through(field: str, value):
    assert _redact({field: value})[field] == value


def test_structural_keys_untouched():
    event = {"level": "info", "event": "url_created", "timestamp": "t", "logger": "x"}
    assert _redact(event) == event


def test_mixed_event_dict():
    out = _redact(
        {
            "event": "api_key_created",
            "key_id": "abc",
            "key_prefix": "abcd1234",
            "api_key": "spoo_raw",
            "has_password": True,
        }
    )
    assert out["key_id"] == "abc"
    assert out["key_prefix"] == "abcd1234"
    assert out["has_password"] is True
    assert out["api_key"] == REDACTED
