"""Gating + roundtrip tests for the meta_tags field on shorten/PATCH."""

from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.dependencies import get_current_user, get_feature_flag_service, get_url_service
from app.errors import ForbiddenError
from app.schemas.models.url import LinkMetaTags

from .conftest import _build_test_app, _make_url_doc, _make_user

META_BODY = {
    "title": "My Title",
    "description": "My description",
    "image": "https://cdn.example.com/og.png",
    "color": "#FF5733",
}


def _flag_svc(enabled: bool) -> AsyncMock:
    svc = AsyncMock()
    svc.is_enabled = AsyncMock(return_value=enabled)

    async def _require(name: str, user, **_kw) -> None:
        # Mirrors FeatureFlagService.require: raise 403 when disabled.
        if not enabled:
            feature = name.replace("_", " ").capitalize()
            raise ForbiddenError(f"{feature} is not enabled for this account")

    svc.require = AsyncMock(side_effect=_require)
    return svc


def _app(user, url_svc, flag_enabled: bool):
    return _build_test_app(
        {
            get_current_user: lambda: user,
            get_url_service: lambda: url_svc,
            get_feature_flag_service: lambda: _flag_svc(flag_enabled),
        }
    )


# ── POST /api/v1/shorten ─────────────────────────────────────────────────────


def test_shorten_with_meta_tags_requires_auth():
    # 401 for anonymous callers — same contract as geo_rules.
    mock_svc = AsyncMock()
    app = _app(None, mock_svc, flag_enabled=True)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/api/v1/shorten",
            json={"long_url": "https://example.com", "meta_tags": META_BODY},
        )
    assert resp.status_code == 401
    mock_svc.create.assert_not_called()


def test_shorten_with_meta_tags_requires_flag():
    user = _make_user(email_verified=True)
    mock_svc = AsyncMock()
    app = _app(user, mock_svc, flag_enabled=False)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/api/v1/shorten",
            json={"long_url": "https://example.com", "meta_tags": META_BODY},
        )
    assert resp.status_code == 403
    assert "not enabled" in resp.json()["error"]
    mock_svc.create.assert_not_called()


def test_shorten_with_meta_tags_flag_on_returns_meta_in_response():
    user = _make_user(email_verified=True)
    doc = _make_url_doc(owner_id=user.user_id)
    doc.meta_tags = LinkMetaTags(**META_BODY)
    mock_svc = AsyncMock()
    mock_svc.create = AsyncMock(return_value=(doc, None))
    app = _app(user, mock_svc, flag_enabled=True)
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.post(
            "/api/v1/shorten",
            json={"long_url": "https://example.com", "meta_tags": META_BODY},
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["meta_tags"]["title"] == "My Title"
    assert body["meta_tags"]["color"] == "#FF5733"
    # request DTO reached the service with the field set
    req = mock_svc.create.call_args.args[0]
    assert req.meta_tags is not None and req.meta_tags.title == "My Title"


def test_plain_anonymous_shorten_unaffected():
    doc = _make_url_doc()
    mock_svc = AsyncMock()
    mock_svc.create = AsyncMock(return_value=(doc, None))
    app = _app(None, mock_svc, flag_enabled=False)
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.post("/api/v1/shorten", json={"long_url": "https://example.com"})
    assert resp.status_code == 201
    assert resp.json()["meta_tags"] is None


def test_shorten_meta_tags_validation_error_on_bad_color():
    user = _make_user(email_verified=True)
    app = _app(user, AsyncMock(), flag_enabled=True)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/api/v1/shorten",
            json={
                "long_url": "https://example.com",
                "meta_tags": {"title": "T", "color": "red"},
            },
        )
    assert resp.status_code == 422


# ── PATCH /api/v1/urls/{url_id} ──────────────────────────────────────────────

URL_ID = "5f0f1d7a2f9b3c4d5e6f7a8b"


def test_patch_meta_tags_flag_on():
    user = _make_user(email_verified=True)
    doc = _make_url_doc(owner_id=user.user_id)
    doc.meta_tags = LinkMetaTags(**META_BODY)
    mock_svc = AsyncMock()
    mock_svc.update = AsyncMock(return_value=doc)
    app = _app(user, mock_svc, flag_enabled=True)
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.patch(f"/api/v1/urls/{URL_ID}", json={"meta_tags": META_BODY})
    assert resp.status_code == 200
    assert resp.json()["meta_tags"]["title"] == "My Title"


def test_patch_meta_tags_flag_off_rejected():
    user = _make_user(email_verified=True)
    mock_svc = AsyncMock()
    app = _app(user, mock_svc, flag_enabled=False)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.patch(f"/api/v1/urls/{URL_ID}", json={"meta_tags": META_BODY})
    assert resp.status_code == 403
    mock_svc.update.assert_not_called()


def test_clearing_meta_tags_is_never_gated():
    # A downgraded user (flag off) must still be able to remove their tags.
    user = _make_user(email_verified=True)
    doc = _make_url_doc(owner_id=user.user_id)
    mock_svc = AsyncMock()
    mock_svc.update = AsyncMock(return_value=doc)
    app = _app(user, mock_svc, flag_enabled=False)
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.patch(f"/api/v1/urls/{URL_ID}", json={"meta_tags": None})
    assert resp.status_code == 200
    mock_svc.update.assert_called_once()


def test_patch_without_meta_tags_field_not_gated():
    user = _make_user(email_verified=True)
    doc = _make_url_doc(owner_id=user.user_id)
    mock_svc = AsyncMock()
    mock_svc.update = AsyncMock(return_value=doc)
    app = _app(user, mock_svc, flag_enabled=False)
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.patch(
            f"/api/v1/urls/{URL_ID}", json={"long_url": "https://new.example.com"}
        )
    assert resp.status_code == 200
