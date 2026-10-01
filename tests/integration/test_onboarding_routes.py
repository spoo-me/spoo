"""
Integration tests for the onboarding contract.

GET  /auth/onboarding          -> resume pointer (empty when unset/expired)
PUT  /auth/onboarding          -> persist pointer
POST /auth/onboarding/complete -> stamp onboarded_at, drop pointer

Pointer and completion are separate facts: the pointer is an ephemeral
Redis value, completion is a permanent user-document field.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

from bson import ObjectId
from fastapi.testclient import TestClient

from app.dependencies import (
    CurrentUser,
    get_onboarding_cache,
    get_user_repo,
    require_auth,
)
from app.routes.auth import router as auth_router
from tests.conftest import build_test_app

_USER_ID = ObjectId()


def _make_user() -> CurrentUser:
    return CurrentUser(user_id=_USER_ID, email_verified=True, api_key_doc=None)


def _build(cache: AsyncMock | None = None, user_repo: AsyncMock | None = None):
    app = build_test_app(
        auth_router,
        overrides={
            require_auth: _make_user,
            get_onboarding_cache: lambda: cache or AsyncMock(),
            get_user_repo: lambda: user_repo or AsyncMock(),
        },
    )
    return TestClient(app, raise_server_exceptions=False)


# ── GET: resume pointer ─────────────────────────────────────────────────────


def test_get_empty_when_nothing_stored():
    cache = AsyncMock()
    cache.get = AsyncMock(return_value=None)
    with _build(cache) as client:
        resp = client.get("/auth/onboarding")
    assert resp.status_code == 200
    assert resp.json() == {"step": None, "path": None}


def test_get_returns_stored_pointer():
    cache = AsyncMock()
    cache.get = AsyncMock(return_value={"step": "domain", "path": "links"})
    with _build(cache) as client:
        resp = client.get("/auth/onboarding")
    assert resp.json() == {"step": "domain", "path": "links"}


# ── PUT: persist pointer ────────────────────────────────────────────────────


def test_put_persists_and_echoes():
    cache = AsyncMock()
    with _build(cache) as client:
        resp = client.put("/auth/onboarding", json={"step": "recap", "path": "api"})
    assert resp.status_code == 200
    assert resp.json() == {"step": "recap", "path": "api"}
    cache.set.assert_awaited_once_with(str(_USER_ID), "recap", "api")


def test_put_rejects_unknown_step():
    # "completed" is not a step anymore — completion has its own endpoint.
    with _build() as client:
        resp = client.put("/auth/onboarding", json={"step": "completed"})
    assert resp.status_code == 422


# ── POST /complete: the permanent fact ──────────────────────────────────────


def test_complete_stamps_and_drops_pointer():
    cache = AsyncMock()
    repo = AsyncMock()
    repo.complete_onboarding = AsyncMock(return_value=True)
    with _build(cache, repo) as client:
        resp = client.post("/auth/onboarding/complete", json={"heard_from": "GitHub"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["onboarded_at"] is not None
    args = repo.complete_onboarding.await_args
    assert args.args[0] == _USER_ID
    assert args.args[2] == "GitHub"
    cache.delete.assert_awaited_once_with(str(_USER_ID))


def test_complete_is_idempotent_and_keeps_original_stamp():
    original = datetime(2026, 7, 1, tzinfo=timezone.utc)
    repo = AsyncMock()
    repo.complete_onboarding = AsyncMock(return_value=False)  # already stamped
    doc = AsyncMock()
    doc.onboarded_at = original
    repo.find_by_id = AsyncMock(return_value=doc)
    with _build(user_repo=repo) as client:
        resp = client.post("/auth/onboarding/complete", json={})
    assert resp.status_code == 200
    assert resp.json()["onboarded_at"].startswith("2026-07-01")


def test_complete_first_call_emits_explicit_utc_offset():
    repo = AsyncMock()
    repo.complete_onboarding = AsyncMock(return_value=True)
    with _build(user_repo=repo) as client:
        resp = client.post("/auth/onboarding/complete", json={})
    assert resp.json()["onboarded_at"].endswith("+00:00")


def test_complete_repeat_call_matches_first_call_wire_format():
    # The repeat path echoes the Mongo read-back, which is NAIVE (the
    # client is not tz_aware). The wire form must be identical to the
    # first call's aware stamp — offsetless ISO parses as local time in
    # JS, and a format that flips between calls is worse.
    repo = AsyncMock()
    repo.complete_onboarding = AsyncMock(return_value=False)  # already stamped
    doc = AsyncMock()
    doc.onboarded_at = datetime(2026, 7, 1)  # naive, as read back from Mongo
    repo.find_by_id = AsyncMock(return_value=doc)
    with _build(user_repo=repo) as client:
        resp = client.post("/auth/onboarding/complete", json={})
    assert resp.json()["onboarded_at"] == "2026-07-01T00:00:00+00:00"


def test_complete_rejects_oversized_heard_from():
    with _build() as client:
        resp = client.post("/auth/onboarding/complete", json={"heard_from": "x" * 65})
    assert resp.status_code == 422
