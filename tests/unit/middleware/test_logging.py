"""Unit tests for request logging middleware helpers (_client_tag, _auth_kind)."""

from __future__ import annotations

import pytest
from starlette.requests import Request

from app.middleware.logging import _auth_kind, _client_tag


def _request(headers: dict[str, str] | None = None) -> Request:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": raw_headers,
        "query_string": b"",
    }
    return Request(scope)


# ── _client_tag ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("dashboard", ("dashboard", None)),
        ("landing", ("landing", None)),
        ("snap/2.1.0", ("snap", "2.1.0")),
        ("cli/0.3.0-beta.1", ("cli", "0.3.0-beta.1")),
        ("bot", ("bot", None)),
    ],
)
def test_client_tag_valid(value: str, expected: tuple):
    assert _client_tag(_request({"X-Spoo-Client": value})) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "Dashboard",  # uppercase slug
        "a" * 33,  # slug too long
        "snap/" + "1" * 17,  # version too long
        "snap/2.1.0/extra",
        "sn ap",
        "snap;DROP",
    ],
)
def test_client_tag_invalid_treated_as_absent(value: str):
    assert _client_tag(_request({"X-Spoo-Client": value})) == (None, None)


def test_client_tag_missing_header():
    assert _client_tag(_request()) == (None, None)


def test_client_tag_strips_whitespace():
    assert _client_tag(_request({"X-Spoo-Client": " raycast "})) == (
        "raycast",
        None,
    )


# ── _auth_kind ───────────────────────────────────────────────────────────────


def test_auth_kind_api_key():
    req = _request({"Authorization": "Bearer spoo_abc123"})
    assert _auth_kind(req) == "api_key"


def test_auth_kind_jwt_bearer():
    req = _request({"Authorization": "Bearer aaa.bbb.ccc"})
    assert _auth_kind(req) == "jwt"


def test_auth_kind_bearer_other():
    req = _request({"Authorization": "Bearer something-else"})
    assert _auth_kind(req) == "bearer_other"


def test_auth_kind_access_token_cookie():
    req = _request({"Cookie": "access_token=aaa.bbb.ccc"})
    assert _auth_kind(req) == "jwt_cookie"


def test_auth_kind_legacy_session_cookie():
    req = _request({"Cookie": "session=xyz"})
    assert _auth_kind(req) == "session_cookie"


def test_auth_kind_access_token_beats_session_cookie():
    req = _request({"Cookie": "session=xyz; access_token=aaa.bbb.ccc"})
    assert _auth_kind(req) == "jwt_cookie"


def test_auth_kind_bearer_beats_cookie():
    req = _request(
        {"Authorization": "Bearer spoo_abc", "Cookie": "access_token=aaa.bbb.ccc"}
    )
    assert _auth_kind(req) == "api_key"


def test_auth_kind_anonymous():
    assert _auth_kind(_request()) == "anonymous"


def test_auth_kind_lowercase_bearer_scheme():
    # Must match dependencies/auth.py, which accepts the scheme
    # case-insensitively; a lowercase bearer API key with a session
    # cookie present must not be classified as jwt_cookie.
    req = _request(
        {"Authorization": "bearer spoo_abc123", "Cookie": "access_token=a.b.c"}
    )
    assert _auth_kind(req) == "api_key"


# ── request_completed identity via request.state.auth_ctx ───────────────────


def _logged_kwargs(mock_log) -> dict:
    for name in ("info", "warning", "error"):
        calls = getattr(mock_log, name).call_args_list
        for call in calls:
            if call.args and call.args[0] == "request_completed":
                return call.kwargs
    raise AssertionError("request_completed was not logged")


def _run_app(set_auth_ctx: bool):
    from unittest.mock import patch

    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from app.middleware.logging import RequestLoggingMiddleware

    async def endpoint(request):
        if set_auth_ctx:
            request.state.auth_ctx = {
                "user_id": "u1",
                "auth_method": "api_key",
                "key_id": "k1",
                "key_prefix": "abcd1234",
            }
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/x", endpoint)])
    app.add_middleware(RequestLoggingMiddleware)

    with patch("app.middleware.logging.log") as mock_log:
        TestClient(app).get("/x")
        return _logged_kwargs(mock_log)


def test_request_completed_includes_auth_ctx():
    kwargs = _run_app(set_auth_ctx=True)
    assert kwargs["user_id"] == "u1"
    assert kwargs["auth_method"] == "api_key"
    assert kwargs["key_id"] == "k1"
    assert kwargs["key_prefix"] == "abcd1234"


def test_request_completed_anonymous_has_no_identity_fields():
    kwargs = _run_app(set_auth_ctx=False)
    assert "user_id" not in kwargs
    assert "auth_method" not in kwargs
