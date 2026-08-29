"""Tests for /api/v1/webhooks — endpoint CRUD, test sends, delivery log.

The service under test is REAL end to end: real WebhookService, real
DeliveryExecutor (renders + signs for real), real matcher/registry — over
in-memory repo fakes. Only outbound HTTP (post_public) and creation-time
SSRF resolution are patched; the flag service and auth are injected.

Wire shapes here are FROZEN — the dashboard webhooks page builds against
the exact bodies asserted in this file.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

from dependencies import get_current_user, get_feature_flag_service, get_webhook_service
from errors import ForbiddenError
from infrastructure.safe_fetch import PostResult
from middleware.rate_limiter import limiter
from routes.api_v1 import router as api_v1_router
from schemas.enums.webhook import DeliveryStatus, WebhookStatus
from schemas.models.webhook import (
    WebhookDeliveryDoc,
    WebhookEndpointDoc,
    WebhookEventDoc,
)
from services.webhooks import DeliveryExecutor, OwnerSubscriptionCache, WebhookService
from services.webhooks.renderers import default_renderers
from services.webhooks.signing import verify
from tests.conftest import build_test_app

from .conftest import _make_api_key_doc, _make_user

_URL = "/api/v1/webhooks"
_MASTER = "test-master-secret"
_HOOK_URL = "https://example.com/hooks/spoo"


@pytest.fixture(autouse=True)
def _reset_limiter_between_tests():
    limiter.reset()
    yield
    limiter.reset()


# ── In-memory repo fakes (duck-typed; services never isinstance-check) ───────


class _FakeEndpointRepo:
    def __init__(self) -> None:
        self.docs: dict[ObjectId, WebhookEndpointDoc] = {}

    async def insert_endpoint(self, doc: WebhookEndpointDoc) -> ObjectId:
        oid = ObjectId()
        stored = doc.model_copy()
        stored.id = oid
        self.docs[oid] = stored
        return oid

    async def find_by_id(self, endpoint_id):
        return self.docs.get(endpoint_id)

    async def find_owned(self, endpoint_id, user_id):
        doc = self.docs.get(endpoint_id)
        return doc if doc is not None and doc.user_id == user_id else None

    async def find_by_user(self, user_id):
        return [d for d in self.docs.values() if d.user_id == user_id]

    async def find_active_for_owner(self, owner_id):
        return [
            d
            for d in self.docs.values()
            if d.user_id == owner_id and d.status == WebhookStatus.ACTIVE
        ]

    async def count_by_user(self, user_id):
        return len(await self.find_by_user(user_id))

    async def update_fields(self, endpoint_id, fields):
        doc = self.docs[endpoint_id]
        merged = doc.model_dump(by_alias=True)
        merged.update(fields)
        merged["_id"] = endpoint_id
        self.docs[endpoint_id] = WebhookEndpointDoc.from_mongo(merged)
        return True

    async def delete_endpoint(self, endpoint_id, user_id):
        doc = self.docs.get(endpoint_id)
        if doc is None or doc.user_id != user_id:
            return False
        del self.docs[endpoint_id]
        return True

    async def record_success(self, endpoint_id):
        doc = self.docs[endpoint_id]
        dropped = doc.dropped_count
        await self.update_fields(
            endpoint_id,
            {
                "consecutive_failures": 0,
                "dropped_count": 0,
                "total_successes": doc.total_successes + 1,
                "last_delivery_at": datetime.now(timezone.utc),
                "last_success_at": datetime.now(timezone.utc),
            },
        )
        return dropped

    async def record_exhausted(self, endpoint_id, reason):
        doc = self.docs[endpoint_id]
        streak = doc.consecutive_failures + 1
        await self.update_fields(
            endpoint_id,
            {
                "consecutive_failures": streak,
                "last_failure_reason": reason,
            },
        )
        return streak

    async def increment_deliveries(self, endpoint_id, by=1):
        doc = self.docs[endpoint_id]
        await self.update_fields(
            endpoint_id, {"total_deliveries": doc.total_deliveries + by}
        )

    async def increment_dropped(self, endpoint_id):
        doc = self.docs[endpoint_id]
        await self.update_fields(endpoint_id, {"dropped_count": doc.dropped_count + 1})

    async def disable(self, endpoint_id, reason):
        await self.update_fields(
            endpoint_id,
            {"status": WebhookStatus.DISABLED.value, "disabled_reason": reason.value},
        )
        return True


class _FakeDeliveryRepo:
    def __init__(self) -> None:
        self.docs: dict[ObjectId, WebhookDeliveryDoc] = {}

    async def insert_row(self, row: dict) -> ObjectId:
        oid = ObjectId()
        row = dict(row)
        row["_id"] = oid
        self.docs[oid] = WebhookDeliveryDoc.from_mongo(row)
        return oid

    async def insert_many_rows(self, rows: list[dict]) -> None:
        for row in rows:
            await self.insert_row(row)

    async def find_by_id(self, delivery_id):
        return self.docs.get(delivery_id)

    async def find_owned(self, delivery_id, user_id):
        doc = self.docs.get(delivery_id)
        return doc if doc is not None and doc.user_id == user_id else None

    async def count_pending(self, endpoint_id):
        return sum(
            1
            for d in self.docs.values()
            if d.endpoint_id == endpoint_id and d.status == DeliveryStatus.PENDING
        )

    async def list_by_endpoint(self, endpoint_id, *, page, page_size, status=None):
        rows = [
            d
            for d in self.docs.values()
            if d.endpoint_id == endpoint_id and (status is None or d.status == status)
        ]
        rows.sort(key=lambda d: d.created_at or datetime.min, reverse=True)
        start = (page - 1) * page_size
        return rows[start : start + page_size], len(rows)

    def _update(self, delivery_id, **fields):
        merged = self.docs[delivery_id].model_dump(by_alias=True)
        merged.update(fields)
        merged["_id"] = delivery_id
        self.docs[delivery_id] = WebhookDeliveryDoc.from_mongo(merged)

    async def set_rendered_body(self, delivery_id, body):
        if self.docs[delivery_id].rendered_body is None:
            self._update(delivery_id, rendered_body=body)

    async def record_attempt_and_reschedule(
        self, delivery_id, attempt, next_attempt_at
    ):
        doc = self.docs[delivery_id]
        self._update(
            delivery_id,
            attempts=[*[a.model_dump() for a in doc.attempts], attempt.model_dump()],
            attempt_count=doc.attempt_count + 1,
            next_attempt_at=next_attempt_at,
            claimed_until=None,
        )

    async def record_attempt_and_finish(self, delivery_id, attempt, status):
        doc = self.docs[delivery_id]
        self._update(
            delivery_id,
            attempts=[*[a.model_dump() for a in doc.attempts], attempt.model_dump()],
            attempt_count=doc.attempt_count + 1,
            status=status.value,
            completed_at=datetime.now(timezone.utc),
            next_attempt_at=None,
        )

    async def mark_failed(self, delivery_id, reason):
        self._update(delivery_id, status=DeliveryStatus.FAILED.value)


class _FakeEventRepo:
    def __init__(self) -> None:
        self.docs: dict[ObjectId, WebhookEventDoc] = {}

    async def insert_event(self, event, *, is_test: bool = False) -> ObjectId:
        oid = ObjectId()
        self.docs[oid] = WebhookEventDoc(
            **{
                "_id": oid,
                "event_id": event.event_id,
                "type": event.type,
                "v": event.v,
                "owner_id": ObjectId(event.owner_id),
                "occurred_at": event.occurred_at,
                "payload": event.data,
                "is_test": is_test,
                "created_at": datetime.now(timezone.utc),
            }
        )
        return oid

    async def find_by_oid(self, oid):
        return self.docs.get(oid)


class _AllowFlags:
    async def require(self, name, user):
        return None


class _DenyFlags:
    async def require(self, name, user):
        raise ForbiddenError("This feature is not available on your account")


# ── Harness ───────────────────────────────────────────────────────────────────


def _build(max_endpoints: int = 5, flags: Any | None = None):
    endpoint_repo = _FakeEndpointRepo()
    delivery_repo = _FakeDeliveryRepo()
    event_repo = _FakeEventRepo()
    executor = DeliveryExecutor(
        delivery_repo,
        endpoint_repo,
        event_repo,
        default_renderers(),
        master_secret=_MASTER,
    )
    service = WebhookService(
        endpoint_repo,
        delivery_repo,
        event_repo,
        executor,
        OwnerSubscriptionCache(None, ttl_seconds=60),
        master_secret=_MASTER,
        max_endpoints=max_endpoints,
    )
    user = _make_user()
    app = build_test_app(
        api_v1_router,
        overrides={
            get_webhook_service: lambda: service,
            get_current_user: lambda: user,
            get_feature_flag_service: lambda: flags or _AllowFlags(),
        },
    )
    return app, user, endpoint_repo, delivery_repo


def _create_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"url": _HOOK_URL, "events": ["link.clicked"]}
    body.update(overrides)
    return body


_NO_SSRF = patch("services.webhooks.service.validate_public_https_url", new=AsyncMock())
_POST_OK = patch(
    "services.webhooks.executor.post_public",
    new=AsyncMock(return_value=PostResult(204, None, None)),
)


# ── Create / list / secret handling ──────────────────────────────────────────


def test_create_returns_secret_exactly_once():
    app, _, _, _ = _build()
    with _NO_SSRF, TestClient(app) as c:
        created = c.post(_URL, json=_create_body())
        assert created.status_code == 201
        body = created.json()
        assert body["signing_secret"].startswith("whsec_")
        assert body["signing_secret_prefix"] == body["signing_secret"][:14]
        assert body["events"] == ["link.clicked"]
        assert body["scope_links"] is None  # all links
        assert body["flavor"] == "raw"
        assert body["status"] == "active"

        listed = c.get(_URL)
        assert listed.status_code == 200
        (entry,) = listed.json()["endpoints"]
        assert "signing_secret" not in entry
        assert entry["signing_secret_prefix"] == body["signing_secret_prefix"]


def test_create_flag_denied_is_403_not_404():
    app, _, _, _ = _build(flags=_DenyFlags())
    with _NO_SSRF, TestClient(app) as c:
        resp = c.post(_URL, json=_create_body())
    assert resp.status_code == 403


def test_create_unknown_event_gets_typo_suggestion():
    app, _, _, _ = _build()
    with _NO_SSRF, TestClient(app) as c:
        resp = c.post(_URL, json=_create_body(events=["link.clickd"]))
    assert resp.status_code == 400
    assert "link.clicked" in resp.json()["error"]


def test_create_over_quota_rejected():
    app, _, _, _ = _build(max_endpoints=1)
    with _NO_SSRF, TestClient(app) as c:
        assert c.post(_URL, json=_create_body()).status_code == 201
        resp = c.post(_URL, json=_create_body())
    assert resp.status_code == 400
    assert "limit" in resp.json()["error"].lower()


def test_create_rejects_non_https_url():
    # No SSRF patch — the real validator rejects http before any DNS work.
    app, _, _, _ = _build()
    with TestClient(app) as c:
        resp = c.post(_URL, json=_create_body(url="http://example.com/hook"))
    assert resp.status_code == 400
    assert "https" in resp.json()["error"]


def test_event_catalog_is_public_and_complete():
    app, _, _, _ = _build()
    # No auth override needed — but user is injected anyway; the route has
    # no auth dependency, which is the property under test via no scopes.
    with TestClient(app) as c:
        resp = c.get(f"{_URL}/event-types")
    assert resp.status_code == 200
    types = resp.json()["event_types"]
    assert len(types) == 6
    clicked = next(t for t in types if t["type"] == "link.clicked")
    assert clicked["category"] == "link"
    assert clicked["sample"]["alias"] == "summer-drop"


# ── Test sends (real executor, patched HTTP) ─────────────────────────────────


def test_send_test_delivers_signed_sample_synchronously():
    app, _, _, _ = _build()
    post = AsyncMock(return_value=PostResult(204, None, None))
    with (
        _NO_SSRF,
        patch("services.webhooks.executor.post_public", post),
        TestClient(app) as c,
    ):
        created = c.post(_URL, json=_create_body()).json()
        secret = created["signing_secret"]

        resp = c.post(
            f"{_URL}/{created['id']}/test", json={"event_type": "link.clicked"}
        )
        assert resp.status_code == 200
        delivery = resp.json()
        assert delivery["is_test"] is True
        assert delivery["status"] == "success"
        assert delivery["event_type"] == "link.clicked"
        assert delivery["attempt_count"] == 1
        assert delivery["attempts"][0]["status_code"] == 204

        # The POST that left the building verifies against the secret the
        # user was shown — the full Standard Webhooks loop, end to end.
        url, body = post.await_args[0]
        headers = post.await_args.kwargs["headers"]
        assert url == _HOOK_URL
        assert verify(
            headers["webhook-id"],
            int(headers["webhook-timestamp"]),
            body,
            secret,
            headers["webhook-signature"],
        )


def test_send_test_flag_denied_403():
    """Test sends initiate outbound calls, so they carry the create gate."""
    app, _, _endpoint_repo, _ = _build()
    with _NO_SSRF, TestClient(app) as c:
        created = c.post(_URL, json=_create_body()).json()
    app2, _, _, _ = _build(flags=_DenyFlags())
    # Same service state is not shared across builds; recreate inline:
    # simpler to flip flags on one app via override swap.
    del app2
    app.dependency_overrides[_flag_override_key()] = lambda: _DenyFlags()
    with _POST_OK, TestClient(app) as c:
        resp = c.post(f"{_URL}/{created['id']}/test", json={})
    assert resp.status_code == 403


def _flag_override_key():
    from dependencies import get_feature_flag_service

    return get_feature_flag_service


def test_send_test_unknown_type_400():
    app, _, _, _ = _build()
    with _NO_SSRF, _POST_OK, TestClient(app) as c:
        created = c.post(_URL, json=_create_body()).json()
        resp = c.post(f"{_URL}/{created['id']}/test", json={"event_type": "nope"})
    assert resp.status_code == 400


# ── Delivery log + retry ─────────────────────────────────────────────────────


def test_delivery_log_lists_test_send():
    app, _, _, _ = _build()
    with _NO_SSRF, _POST_OK, TestClient(app) as c:
        created = c.post(_URL, json=_create_body()).json()
        c.post(f"{_URL}/{created['id']}/test", json={})

        resp = c.get(f"{_URL}/{created['id']}/deliveries")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        (row,) = body["deliveries"]
        assert row["webhook_id"].startswith("msg_")
        assert row["rendered_body"] is not None
        assert row["status"] == "success"


def test_manual_retry_reuses_webhook_id():
    app, _, _, _ = _build()
    with _NO_SSRF, _POST_OK, TestClient(app) as c:
        created = c.post(_URL, json=_create_body()).json()
        first = c.post(f"{_URL}/{created['id']}/test", json={}).json()
        retried = c.post(
            f"{_URL}/{created['id']}/deliveries/{first['id']}/retry"
        ).json()
    assert retried["webhook_id"] == first["webhook_id"]
    assert retried["attempt_count"] == 2


# ── Update / pause / delete ──────────────────────────────────────────────────


def test_patch_pause_and_resume():
    app, _, _, _ = _build()
    with _NO_SSRF, TestClient(app) as c:
        created = c.post(_URL, json=_create_body()).json()
        paused = c.patch(f"{_URL}/{created['id']}", json={"status": "paused"})
        assert paused.json()["status"] == "paused"
        resumed = c.patch(f"{_URL}/{created['id']}", json={"status": "active"})
        assert resumed.json()["status"] == "active"


def test_patch_disabled_is_rejected():
    app, _, _, _ = _build()
    with _NO_SSRF, TestClient(app) as c:
        created = c.post(_URL, json=_create_body()).json()
        resp = c.patch(f"{_URL}/{created['id']}", json={"status": "disabled"})
    assert resp.status_code == 400


def test_delete_endpoint_204_then_404():
    app, _, _, _ = _build()
    with _NO_SSRF, TestClient(app) as c:
        created = c.post(_URL, json=_create_body()).json()
        assert c.delete(f"{_URL}/{created['id']}").status_code == 204
        assert c.get(f"{_URL}/{created['id']}").status_code == 404


def test_scoped_endpoint_roundtrips_link_ids():
    app, _, _, _ = _build()
    link_id = str(ObjectId())
    with _NO_SSRF, TestClient(app) as c:
        created = c.post(_URL, json=_create_body(scope_links=[link_id])).json()
    assert created["scope_links"] == [link_id]


def test_send_test_renders_discord_flavor_and_signature_covers_it():
    """A discord-flavor endpoint delivers an embed body, and the signature
    verifies over that rendered body (the raw envelope never leaves)."""
    app, _, _, _ = _build()
    post = AsyncMock(return_value=PostResult(204, None, None))
    with (
        _NO_SSRF,
        patch("services.webhooks.executor.post_public", post),
        TestClient(app) as c,
    ):
        created = c.post(_URL, json=_create_body(flavor="discord")).json()
        assert created["flavor"] == "discord"
        secret = created["signing_secret"]

        resp = c.post(
            f"{_URL}/{created['id']}/test", json={"event_type": "link.clicked"}
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

        url, body = post.await_args[0]
        headers = post.await_args.kwargs["headers"]
        assert url.endswith("?with_components=true")
        sent = json.loads(body)
        assert sent["username"] == "spoo.me"
        assert sent["flags"] == 32768
        texts = [
            c["content"] for c in sent["components"][0]["components"] if c["type"] == 10
        ]
        assert texts[0].startswith("### Click")
        assert verify(
            headers["webhook-id"],
            int(headers["webhook-timestamp"]),
            body,
            secret,
            headers["webhook-signature"],
        )


def test_create_rejects_unknown_flavor_422():
    app, _, _, _ = _build()
    with _NO_SSRF, TestClient(app) as c:
        resp = c.post(_URL, json=_create_body(flavor="teams"))
        assert resp.status_code == 422


def test_reveal_secret_returns_the_created_secret():
    app, _, _, _ = _build()
    with _NO_SSRF, TestClient(app) as c:
        created = c.post(_URL, json=_create_body()).json()
        revealed = c.get(f"{_URL}/{created['id']}/secret")
        assert revealed.status_code == 200
        assert revealed.json()["signing_secret"] == created["signing_secret"]


def test_reveal_secret_unknown_endpoint_404():
    app, _, _, _ = _build()
    with _NO_SSRF, TestClient(app) as c:
        resp = c.get(f"{_URL}/{ObjectId()}/secret")
        assert resp.status_code == 404


def test_reveal_secret_refuses_api_key_auth():
    """Secret reveal is credential material: interactive sessions only,
    like key creation."""
    app, _, _, _ = _build()
    with _NO_SSRF, TestClient(app) as c:
        created = c.post(_URL, json=_create_body()).json()
    key_user = _make_user(api_key_doc=_make_api_key_doc(scopes=["webhooks:manage"]))
    app.dependency_overrides[get_current_user] = lambda: key_user
    with TestClient(app) as c:
        resp = c.get(f"{_URL}/{created['id']}/secret")
        assert resp.status_code == 403
