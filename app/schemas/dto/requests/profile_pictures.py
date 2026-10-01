"""Request DTOs for profile picture endpoints."""

from __future__ import annotations

from pydantic import Field

from app.schemas.dto.base import RequestBase


class SetProfilePictureRequest(RequestBase):
    picture_id: str = Field(min_length=1, max_length=200)


class UploadProfilePictureRequest(RequestBase):
    # Size/type gates run in the service against the configured cap
    # (R2_UPLOAD_MAX_BYTES) — same posture as meta-tag image uploads.
    image: str = Field(
        min_length=1, description="base64 data URI (image/png, image/jpeg, image/webp)"
    )
