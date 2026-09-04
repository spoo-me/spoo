"""Unit tests for ProfilePictureService."""

from __future__ import annotations

import base64
import os
import struct
import zlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/")

from bson import ObjectId

from errors import NotFoundError, ValidationError
from services.profile_picture_service import ProfilePictureService


def _make_provider(provider="google", picture="https://img.example.com/pic.jpg"):
    p = MagicMock()
    p.provider = provider
    p.provider_user_id = "uid123"
    p.email = "user@example.com"
    p.linked_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    p.profile = MagicMock()
    p.profile.picture = picture
    return p


def _make_user_doc(pfp_url=None, providers=None, storage_prefix=None):
    doc = MagicMock()
    doc.id = ObjectId()
    doc.email = "user@example.com"
    doc.storage_prefix = storage_prefix
    doc.email_verified = True
    doc.user_name = "testuser"
    doc.password_set = True
    doc.auth_providers = providers or []
    if pfp_url:
        doc.pfp = MagicMock()
        doc.pfp.url = pfp_url
        doc.pfp.source = "google"
    else:
        doc.pfp = None
    return doc


def _make_service(user_doc=None, storage=None):
    repo = MagicMock()
    repo.find_by_id = AsyncMock(return_value=user_doc)
    repo.update = AsyncMock()
    repo.set_storage_prefix_if_absent = AsyncMock(return_value=True)
    return ProfilePictureService(repo, r2_storage=storage), repo


def _png_bytes(width=3, height=2) -> bytes:
    ihdr = struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    chunk = (
        struct.pack(">I", 13)
        + b"IHDR"
        + ihdr
        + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
    )
    return b"\x89PNG\r\n\x1a\n" + chunk


def _data_uri(data: bytes, mime="image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def _storage(configured=True) -> MagicMock:
    s = MagicMock()
    s.is_configured = configured
    s.put_object = AsyncMock(
        return_value="https://og.spoo.me/profile-pictures/x/abc.png"
    )
    return s


# ── get_dashboard_profile ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_dashboard_profile_returns_correct_shape():
    provider = _make_provider()
    user = _make_user_doc(
        pfp_url="https://img.example.com/pic.jpg", providers=[provider]
    )
    svc, _ = _make_service(user)
    profile = await svc.get_dashboard_profile(user.id)
    assert profile["email"] == "user@example.com"
    assert profile["email_verified"] is True
    assert profile["password_set"] is True
    assert len(profile["auth_providers"]) == 1
    assert profile["auth_providers"][0]["provider"] == "google"
    assert "pfp" in profile
    assert profile["pfp"]["url"] == "https://img.example.com/pic.jpg"


@pytest.mark.asyncio
async def test_get_dashboard_profile_no_pfp():
    user = _make_user_doc()
    svc, _ = _make_service(user)
    profile = await svc.get_dashboard_profile(user.id)
    assert "pfp" not in profile


@pytest.mark.asyncio
async def test_get_dashboard_profile_user_not_found():
    svc, _ = _make_service(None)
    result = await svc.get_dashboard_profile(ObjectId())
    assert result is None


# ── get_available_pictures ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_available_pictures_returns_provider_pictures():
    p1 = _make_provider("google", "https://google.com/pic.jpg")
    p2 = _make_provider("github", "https://github.com/pic.jpg")
    user = _make_user_doc(providers=[p1, p2])
    svc, _ = _make_service(user)
    pics = await svc.get_available_pictures(user.id)
    assert len(pics) == 2
    assert pics[0].source == "google"
    assert pics[1].source == "github"


@pytest.mark.asyncio
async def test_get_available_pictures_marks_current():
    p = _make_provider("google", "https://google.com/pic.jpg")
    user = _make_user_doc(pfp_url="https://google.com/pic.jpg", providers=[p])
    svc, _ = _make_service(user)
    pics = await svc.get_available_pictures(user.id)
    assert pics[0].is_current is True


@pytest.mark.asyncio
async def test_get_available_pictures_skips_no_picture():
    p = _make_provider("google", None)
    user = _make_user_doc(providers=[p])
    svc, _ = _make_service(user)
    pics = await svc.get_available_pictures(user.id)
    assert len(pics) == 0


@pytest.mark.asyncio
async def test_get_available_pictures_user_not_found():
    svc, _ = _make_service(None)
    with pytest.raises(NotFoundError):
        await svc.get_available_pictures(ObjectId())


# ── set_picture ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_picture_updates_pfp():
    p = _make_provider("google", "https://google.com/pic.jpg")
    user = _make_user_doc(providers=[p])
    svc, repo = _make_service(user)
    await svc.set_picture(user.id, "google_uid123")
    repo.update.assert_called_once()
    call_args = repo.update.call_args
    assert call_args[0][0] == user.id
    assert "$set" in call_args[0][1]
    assert call_args[0][1]["$set"]["pfp"]["url"] == "https://google.com/pic.jpg"


@pytest.mark.asyncio
async def test_set_picture_invalid_id_raises():
    p = _make_provider("google", "https://google.com/pic.jpg")
    user = _make_user_doc(providers=[p])
    svc, _ = _make_service(user)
    with pytest.raises(NotFoundError, match="Picture not found"):
        await svc.set_picture(user.id, "invalid_id")


@pytest.mark.asyncio
async def test_set_picture_user_not_found_raises():
    svc, _ = _make_service(None)
    with pytest.raises(NotFoundError, match="User not found"):
        await svc.set_picture(ObjectId(), "google_uid123")


@pytest.mark.asyncio
async def test_set_picture_rejects_arbitrary_url():
    """Cannot set a picture_id that doesn't match any auth provider."""
    p = _make_provider("google", "https://google.com/pic.jpg")
    user = _make_user_doc(providers=[p])
    svc, repo = _make_service(user)
    with pytest.raises(NotFoundError):
        await svc.set_picture(user.id, "evil_attacker_id")
    repo.update.assert_not_called()


# ── update_user_name ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_user_name_sets_name():
    user = _make_user_doc()
    svc, repo = _make_service(user)
    await svc.update_user_name(user.id, "Jane Doe")
    repo.update.assert_called_once_with(user.id, {"$set": {"user_name": "Jane Doe"}})


@pytest.mark.asyncio
async def test_update_user_name_clears_with_none():
    user = _make_user_doc()
    svc, repo = _make_service(user)
    await svc.update_user_name(user.id, None)
    repo.update.assert_called_once_with(user.id, {"$set": {"user_name": None}})


@pytest.mark.asyncio
async def test_update_user_name_user_not_found_raises():
    svc, repo = _make_service(None)
    with pytest.raises(NotFoundError, match="User not found"):
        await svc.update_user_name(ObjectId(), "Jane Doe")
    repo.update.assert_not_called()


# ── upload_picture ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_picture_stores_r2_url_with_upload_source():
    user = _make_user_doc()
    storage = _storage()
    svc, repo = _make_service(user, storage=storage)
    await svc.upload_picture(user.id, _data_uri(_png_bytes()))
    storage.put_object.assert_awaited_once()
    key = storage.put_object.call_args[0][0]
    assert key.startswith("profile-pictures/")
    assert key.endswith(".png")
    pfp = repo.update.call_args[0][1]["$set"]["pfp"]
    assert pfp["url"] == "https://og.spoo.me/profile-pictures/x/abc.png"
    assert pfp["source"] == "upload"


@pytest.mark.asyncio
async def test_upload_picture_pins_storage_prefix_on_first_use():
    """First upload persists the computed prefix — a later SECRET_KEY
    rotation must not orphan the objects for the erasure sweep."""
    from services.image_ingest import owner_key_prefix

    user = _make_user_doc()  # storage_prefix=None → first upload
    svc, repo = _make_service(user, storage=_storage())
    svc._key_secret = "sekret"
    await svc.upload_picture(user.id, _data_uri(_png_bytes()))
    expected = owner_key_prefix(user.id, "sekret")
    repo.set_storage_prefix_if_absent.assert_awaited_once_with(user.id, expected)


@pytest.mark.asyncio
async def test_upload_picture_uses_the_pinned_prefix():
    user = _make_user_doc(storage_prefix="p1nn3dpr3f1x0000")
    storage = _storage()
    svc, repo = _make_service(user, storage=storage)
    await svc.upload_picture(user.id, _data_uri(_png_bytes()))
    key = storage.put_object.call_args[0][0]
    assert key.startswith("profile-pictures/p1nn3dpr3f1x0000/")
    # Already pinned — no second write.
    repo.set_storage_prefix_if_absent.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_picture_rejects_non_data_uri():
    user = _make_user_doc()
    svc, repo = _make_service(user, storage=_storage())
    with pytest.raises(ValidationError, match="data URI"):
        await svc.upload_picture(user.id, "https://example.com/pic.png")
    repo.update.assert_not_called()


@pytest.mark.asyncio
async def test_upload_picture_rejects_mime_mismatch():
    # Declared jpeg, bytes are PNG — the security gate.
    user = _make_user_doc()
    svc, repo = _make_service(user, storage=_storage())
    with pytest.raises(ValidationError, match="do not match"):
        await svc.upload_picture(user.id, _data_uri(_png_bytes(), mime="image/jpeg"))
    repo.update.assert_not_called()


@pytest.mark.asyncio
async def test_upload_picture_rejects_oversized():
    user = _make_user_doc()
    svc, repo = _make_service(user, storage=_storage())
    big = _data_uri(b"\x89PNG\r\n\x1a\n" + b"0" * 600_000)
    with pytest.raises(ValidationError, match="exceeds"):
        await svc.upload_picture(user.id, big)
    repo.update.assert_not_called()


@pytest.mark.asyncio
async def test_upload_picture_storage_unconfigured_raises():
    user = _make_user_doc()
    svc, repo = _make_service(user, storage=None)
    with pytest.raises(ValidationError, match="not available"):
        await svc.upload_picture(user.id, _data_uri(_png_bytes()))
    repo.update.assert_not_called()


@pytest.mark.asyncio
async def test_upload_picture_user_not_found_raises():
    svc, _ = _make_service(None, storage=_storage())
    with pytest.raises(NotFoundError, match="User not found"):
        await svc.upload_picture(ObjectId(), _data_uri(_png_bytes()))


# ── unset_picture ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unset_picture_clears_pfp():
    user = _make_user_doc(pfp_url="https://img.example.com/pic.jpg")
    svc, repo = _make_service(user)
    await svc.unset_picture(user.id)
    repo.update.assert_called_once_with(user.id, {"$set": {"pfp": None}})


@pytest.mark.asyncio
async def test_unset_picture_idempotent_when_no_pfp():
    user = _make_user_doc()
    svc, repo = _make_service(user)
    await svc.unset_picture(user.id)
    repo.update.assert_called_once_with(user.id, {"$set": {"pfp": None}})


@pytest.mark.asyncio
async def test_unset_picture_user_not_found_raises():
    svc, _ = _make_service(None)
    with pytest.raises(NotFoundError, match="User not found"):
        await svc.unset_picture(ObjectId())
