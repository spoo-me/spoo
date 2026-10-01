"""/api/v1/me/profile-pictures — list, set, upload and unset the profile picture."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from app.dependencies import (
    get_current_user,
    get_profile_picture_service,
    require_auth,
    require_jwt,
)
from app.errors import NotFoundError, ValidationError
from app.services.profile_picture_service import ProfilePictureService

from .conftest import _build_test_app, _make_api_key_doc, _make_user

_PICTURES = [
    {
        "id": "google_uid",
        "url": "https://pic.example/a.jpg",
        "source": "google",
        "is_current": True,
    }
]


def _mock_svc(pictures=None):
    svc = MagicMock(spec=ProfilePictureService)
    svc.get_available_pictures = AsyncMock(return_value=pictures or [])
    svc.set_picture = AsyncMock()
    svc.upload_picture = AsyncMock()
    svc.unset_picture = AsyncMock()
    return svc


def _app(user, svc=None):
    return _build_test_app(
        {
            require_jwt: lambda: user,
            get_current_user: lambda: user,
            get_profile_picture_service: lambda: svc or _mock_svc(),
        }
    )


# ── Auth gating ──────────────────────────────────────────────────────────────


def test_all_endpoints_require_auth():
    # No auth override at all: the real require_jwt runs and rejects.
    app = _build_test_app({get_profile_picture_service: lambda: _mock_svc()})
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/api/v1/me/profile-pictures").status_code == 401
        assert (
            client.post(
                "/api/v1/me/profile-pictures", json={"picture_id": "x"}
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/api/v1/me/profile-pictures/upload",
                json={"image": "data:image/png;base64,aGk="},
            ).status_code
            == 401
        )
        assert client.delete("/api/v1/me/profile-pictures").status_code == 401


def test_all_endpoints_reject_api_key_auth():
    # /me/* is JWT-only: a request that authenticates with a valid API key
    # (any scope) must get 403 from the real require_jwt, matching the
    # features/layouts siblings and PATCH /auth/me.
    user = _make_user(api_key_doc=_make_api_key_doc())
    svc = _mock_svc()
    app = _build_test_app(
        {
            require_auth: lambda: user,
            get_profile_picture_service: lambda: svc,
        }
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/api/v1/me/profile-pictures").status_code == 403
        assert (
            client.post(
                "/api/v1/me/profile-pictures", json={"picture_id": "x"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/v1/me/profile-pictures/upload",
                json={"image": "data:image/png;base64,aGk="},
            ).status_code
            == 403
        )
        assert client.delete("/api/v1/me/profile-pictures").status_code == 403
    svc.set_picture.assert_not_called()
    svc.upload_picture.assert_not_called()
    svc.unset_picture.assert_not_called()


# ── GET (available pictures) ─────────────────────────────────────────────────


def test_get_returns_available_pictures():
    user = _make_user()
    svc = _mock_svc(pictures=_PICTURES)
    with TestClient(_app(user, svc)) as client:
        resp = client.get("/api/v1/me/profile-pictures")
    assert resp.status_code == 200
    assert resp.json()["pictures"] == _PICTURES
    svc.get_available_pictures.assert_called_once_with(user.user_id)


# ── POST (set provider picture) ──────────────────────────────────────────────


def test_set_picture_success():
    user = _make_user()
    svc = _mock_svc()
    with TestClient(_app(user, svc)) as client:
        resp = client.post(
            "/api/v1/me/profile-pictures", json={"picture_id": "google_uid"}
        )
    assert resp.status_code == 200
    assert resp.json()["message"] == "Profile picture updated successfully"
    svc.set_picture.assert_called_once_with(user.user_id, "google_uid")


def test_set_picture_missing_id_returns_422():
    user = _make_user()
    with TestClient(_app(user)) as client:
        resp = client.post("/api/v1/me/profile-pictures", json={})
    assert resp.status_code == 422


def test_set_picture_unknown_id_returns_404():
    user = _make_user()
    svc = _mock_svc()
    svc.set_picture = AsyncMock(side_effect=NotFoundError("Picture not found"))
    with TestClient(_app(user, svc)) as client:
        resp = client.post("/api/v1/me/profile-pictures", json={"picture_id": "bad_id"})
    assert resp.status_code == 404


# ── POST /upload (custom picture) ────────────────────────────────────────────


def test_upload_success():
    user = _make_user()
    svc = _mock_svc()
    with TestClient(_app(user, svc)) as client:
        resp = client.post(
            "/api/v1/me/profile-pictures/upload",
            json={"image": "data:image/png;base64,aGk="},
        )
    assert resp.status_code == 200
    assert resp.json()["message"] == "Profile picture updated successfully"
    svc.upload_picture.assert_called_once_with(
        user.user_id, "data:image/png;base64,aGk="
    )


def test_upload_missing_image_returns_422():
    user = _make_user()
    with TestClient(_app(user)) as client:
        resp = client.post("/api/v1/me/profile-pictures/upload", json={})
    assert resp.status_code == 422


def test_upload_invalid_image_returns_400():
    user = _make_user()
    svc = _mock_svc()
    svc.upload_picture = AsyncMock(
        side_effect=ValidationError(
            "image bytes do not match the declared image type", field="image"
        )
    )
    with TestClient(_app(user, svc)) as client:
        resp = client.post(
            "/api/v1/me/profile-pictures/upload",
            json={"image": "data:image/png;base64,aGk="},
        )
    assert resp.status_code == 400
    assert resp.json()["field"] == "image"


# ── DELETE (unset) ───────────────────────────────────────────────────────────


def test_delete_returns_success():
    user = _make_user()
    svc = _mock_svc()
    with TestClient(_app(user, svc)) as client:
        resp = client.delete("/api/v1/me/profile-pictures")
    assert resp.status_code == 200
    assert resp.json()["message"] == "Profile picture removed"
    svc.unset_picture.assert_called_once_with(user.user_id)


def test_delete_is_idempotent():
    # Unsetting when no picture is set is still a 200: the service clears
    # the field unconditionally, so a second delete looks exactly the same.
    user = _make_user()
    svc = _mock_svc()
    with TestClient(_app(user, svc)) as client:
        first = client.delete("/api/v1/me/profile-pictures")
        second = client.delete("/api/v1/me/profile-pictures")
    assert first.status_code == 200
    assert second.status_code == 200
    assert svc.unset_picture.await_count == 2
