"""Response DTOs for profile picture endpoints."""

from __future__ import annotations

from app.schemas.dto.base import ResponseBase

# AvailablePicture is the service's own serialization shape (plain fields,
# no logic) — reused verbatim so the wire format has a single source of truth.
from app.services.profile_picture_service import AvailablePicture


class AvailablePicturesResponse(ResponseBase):
    pictures: list[AvailablePicture]


class ProfilePictureMessageResponse(ResponseBase):
    message: str
