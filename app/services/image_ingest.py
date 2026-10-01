"""Shared data-URI image ingestion primitives.

Meta-tag og:images and profile-picture uploads accept the same payload
(a base64 data URI) and must enforce the same abuse posture: a size cap,
strict base64, and a magic-byte match against the declared MIME type.
This module owns those gates plus the owner-scoped storage-key prefix;
callers own the surrounding flow (https passthrough, storage-key layout,
error fields).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.errors import ValidationError
from app.schemas.models.base import ANONYMOUS_OWNER_ID
from app.shared.image_sniff import MIME, ImageInfo, sniff_image

if TYPE_CHECKING:
    from bson import ObjectId

    from app.repositories.user_repository import UserRepository

_DATA_URI_RE = re.compile(
    r"^data:image/(?P<fmt>png|jpeg|webp);base64,(?P<b64>[A-Za-z0-9+/=]+)$"
)


@dataclass(frozen=True)
class DecodedImage:
    data: bytes
    content_type: str  # declared MIME, verified against the magic bytes
    info: ImageInfo


def split_image_data_uri(value: str) -> tuple[str, str] | None:
    """Return ``(fmt, b64)`` when ``value`` is a supported image data URI."""
    m = _DATA_URI_RE.match(value)
    if m is None:
        return None
    return m["fmt"], m["b64"]


def decode_image_data_uri(
    fmt: str, b64: str, *, max_bytes: int, field: str
) -> DecodedImage:
    """Decode and validate a data-URI payload; raises ValidationError."""
    if len(b64) > (max_bytes * 4) // 3 + 4:  # cheap pre-decode gate
        raise ValidationError(f"image exceeds {max_bytes} bytes", field=field)
    try:
        data = base64.b64decode(b64, validate=True)
    except binascii.Error:
        raise ValidationError(
            "image data URI is not valid base64", field=field
        ) from None
    if len(data) > max_bytes:
        raise ValidationError(f"image exceeds {max_bytes} bytes", field=field)

    info = sniff_image(data)
    declared_mime = f"image/{fmt}"
    if info is None or MIME[info.format] != declared_mime:
        # Magic bytes are the security gate — the declared type must match
        # what the bytes actually are.
        raise ValidationError(
            "image bytes do not match the declared image type",
            field=field,
        )
    return DecodedImage(data=data, content_type=declared_mime, info=info)


def owner_key_prefix(owner_id: ObjectId, secret: str) -> str:
    """Non-reversible per-owner path segment for storage keys.

    A raw ObjectId in a public URL leaks the account's creation time and
    lets anyone correlate a user's links; the HMAC keeps per-owner sweeps
    and dedup without exposing it. Rotating SECRET_KEY re-keys future
    uploads (old sweeps need the old secret).
    """
    digest = hmac.new(secret.encode(), str(owner_id).encode(), hashlib.sha256)
    return digest.hexdigest()[:16]


async def resolve_owner_prefix(
    owner_id: ObjectId, secret: str, user_repo: UserRepository | None = None
) -> str:
    """Resolve the owner's storage prefix, pinning it on first use.

    Prefers the prefix persisted on the user doc — the one existing objects
    actually live under — and only computes ``owner_key_prefix`` fresh when
    none is stored yet, pinning that value so a later SECRET_KEY rotation
    can't orphan the objects (uploads made before the field existed still
    orphan on rotation — historical, not fixable retroactively). Anonymous
    owners and repo-less callers get the bare computation: there is no user
    doc to pin anything on.
    """
    computed = owner_key_prefix(owner_id, secret)
    if user_repo is None or owner_id == ANONYMOUS_OWNER_ID:
        return computed
    user = await user_repo.find_by_id(owner_id)
    if user is None:
        return computed
    if user.storage_prefix:
        return user.storage_prefix
    await user_repo.set_storage_prefix_if_absent(owner_id, computed)
    return computed
