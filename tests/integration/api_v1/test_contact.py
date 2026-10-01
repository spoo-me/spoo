"""Tests for POST /api/v1/contact — the JSON twin of the Jinja form.

ContactService is REAL (the endpoint reuses ``send_contact_message``
verbatim) and so is DiscordOpsNotifier — only its HTTP client and the
captcha are capturing fakes, so the legacy Discord embed shape is pinned
exactly. Settings are injected per test to drive the
configured/unconfigured branches.

Note on validation statuses: DTO shape failures (invalid email, empty
message) return 422 with ``code: validation_error`` via the global
handler — the codebase-wide convention every /api/v1 endpoint follows.
The TRD sketched 400 for these; the body shape and ``code`` are
identical, only the status differs. Semantic gates (missing captcha
token when captcha is configured) stay 400.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import AppSettings
from app.dependencies import get_contact_service
from app.infrastructure.ops_notify import DiscordOpsNotifier
from app.middleware.rate_limiter import limiter
from app.routes.api_v1 import router as api_v1_router
from app.services.contact_service import ContactService
from tests.conftest import build_test_app

_URL = "/api/v1/contact"


@pytest.fixture(autouse=True)
def _reset_limiter_between_tests():
    """POST /api/v1/contact rides Limits.CONTACT (5/min) — clear the
    in-memory counters so tests never eat each other's budget."""
    limiter.reset()
    yield
    limiter.reset()


# ── Fakes ─────────────────────────────────────────────────────────────────────


class _CapturingHttp:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.payloads: list[dict[str, Any]] = []

    # Same surface as HttpClient.post(url, **kwargs): the notifier passes
    # follow_redirects and, with an image, data/files instead of json.
    async def post(self, url: str, json: dict[str, Any] | None = None, **kwargs: Any):
        self.payloads.append(json)
        return SimpleNamespace(status_code=204 if self.ok else 500, text="boom")


class _FakeNotifier(DiscordOpsNotifier):
    """The REAL DiscordOpsNotifier over a capturing HTTP fake, so the
    contact embed shape is pinned end to end."""

    def __init__(self, ok: bool = True) -> None:
        self._captured = _CapturingHttp(ok)
        super().__init__("https://hooks.test/contact", "", self._captured)

    @property
    def payloads(self) -> list[dict[str, Any]]:
        return self._captured.payloads


class _FakeCaptcha:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.tokens: list[str] = []

    async def verify(self, token: str) -> bool:
        self.tokens.append(token)
        return self.ok


# ── App builder ───────────────────────────────────────────────────────────────


def _client(
    *,
    contact_webhook: str = "https://hooks.test/contact",
    sitekey: str = "test-sitekey",
    notifier: _FakeNotifier | None = None,
    captcha: _FakeCaptcha | None = None,
) -> TestClient:
    notifier = notifier if notifier is not None else _FakeNotifier()
    captcha = captcha if captcha is not None else _FakeCaptcha()
    service = ContactService(notifier, captcha)
    settings = AppSettings(
        contact_webhook=contact_webhook,
        hcaptcha_sitekey=sitekey,
    )
    app = build_test_app(
        api_v1_router,
        overrides={get_contact_service: lambda: service},
        extra_state={"settings": settings},
    )
    return TestClient(app)


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "email": "reporter@example.com",
        "message": "hello there",
        "captcha_token": "tok",
    }
    body.update(overrides)
    return body


# ── Happy path ────────────────────────────────────────────────────────────────


def test_contact_happy_path_sends_exact_legacy_embed():
    notifier = _FakeNotifier()
    captcha = _FakeCaptcha()
    with _client(notifier=notifier, captcha=captcha) as c:
        resp = c.post(_URL, json=_body())

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    assert captcha.tokens == ["tok"]
    assert len(notifier.payloads) == 1
    embed = notifier.payloads[0]["embeds"][0]
    assert embed["title"] == "New Contact Message ✉️"
    assert embed["color"] == 9103397
    assert embed["fields"] == [
        {"name": "Email", "value": "```reporter@example.com```"},
        {"name": "Message", "value": "```hello there```"},
    ]
    assert embed["footer"] == {
        "text": "spoo-me",
        "icon_url": "https://spoo.me/static/images/favicon.png",
    }
    assert "timestamp" in embed


# ── Captcha semantics (mirror the Jinja form) ─────────────────────────────────


def test_contact_missing_captcha_when_configured_400():
    notifier = _FakeNotifier()
    captcha = _FakeCaptcha()
    with _client(notifier=notifier, captcha=captcha) as c:
        resp = c.post(_URL, json=_body(captcha_token=None))

    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "validation_error"
    assert "captcha" in body["error"].lower()
    # Rejected before any service work.
    assert captcha.tokens == []
    assert notifier.payloads == []


def test_contact_captcha_not_required_when_unconfigured():
    notifier = _FakeNotifier()
    with _client(sitekey="", notifier=notifier) as c:
        resp = c.post(_URL, json=_body(captcha_token=None))

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert len(notifier.payloads) == 1


def test_contact_captcha_failure_403():
    notifier = _FakeNotifier()
    with _client(captcha=_FakeCaptcha(ok=False), notifier=notifier) as c:
        resp = c.post(_URL, json=_body())

    assert resp.status_code == 403
    assert resp.json() == {
        "error": "Invalid captcha, please try again",
        "code": "forbidden",
    }
    assert notifier.payloads == []


# ── Not configured / failure branches ─────────────────────────────────────────


def test_contact_webhook_unset_503():
    with _client(contact_webhook="") as c:
        resp = c.post(_URL, json=_body())

    assert resp.status_code == 503
    body = resp.json()
    assert body["code"] == "not_configured"
    assert body["error"]


def test_contact_notify_send_failure_500():
    with _client(notifier=_FakeNotifier(ok=False)) as c:
        resp = c.post(_URL, json=_body())

    assert resp.status_code == 500
    assert resp.json()["code"] == "internal_error"


# ── Shape validation ──────────────────────────────────────────────────────────


def test_contact_invalid_email_is_validation_error():
    with _client() as c:
        resp = c.post(_URL, json=_body(email="not-an-email"))

    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "validation_error"
    assert body["field"] == "email"


def test_contact_empty_message_is_validation_error():
    with _client() as c:
        resp = c.post(_URL, json=_body(message=""))

    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"


def test_contact_message_over_4000_chars_is_validation_error():
    with _client() as c:
        resp = c.post(_URL, json=_body(message="x" * 4001))

    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"
