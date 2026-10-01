"""
ProfilePictureService — dashboard profile, display name, and profile
picture management.

Extracts user profile building and profile picture logic from the route
layer. Uploaded pictures ride the same R2 plumbing (and the same abuse
posture — size cap, strict base64, magic-byte MIME match) as meta-tag
og:images; see services.image_ingest.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from bson import ObjectId
from pydantic import BaseModel, ConfigDict, Field

from errors import NotFoundError, ValidationError
from infrastructure.logging import get_logger
from repositories.user_repository import UserRepository
from schemas.models.user import OAuthProvider, ProfilePicture, UserDoc
from services.image_ingest import (
    decode_image_data_uri,
    owner_key_prefix,
    split_image_data_uri,
)
from shared.image_sniff import EXT

if TYPE_CHECKING:
    from infrastructure.storage.r2 import R2StorageClient

log = get_logger(__name__)


class AvailablePicture(BaseModel):
    """A profile picture option from a linked OAuth provider."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(description="Unique identifier (provider_providerUserId)")
    url: str = Field(description="Picture URL")
    source: OAuthProvider = Field(description="OAuth provider source")
    is_current: bool = Field(description="Whether this is the active picture")


class ProfilePictureService:
    def __init__(
        self,
        user_repo: UserRepository,
        *,
        r2_storage: R2StorageClient | None = None,
        upload_max_bytes: int = 512_000,
        key_secret: str = "",
    ) -> None:
        self._user_repo = user_repo
        self._r2_storage = r2_storage
        self._upload_max_bytes = upload_max_bytes
        self._key_secret = key_secret

    async def get_dashboard_profile(self, user_id: ObjectId) -> dict | None:
        """Fetch a minimal user profile for dashboard template rendering.

        Returns None if the user is not found.
        Matches the shape produced by Flask's utils/auth_utils.get_user_profile().
        """
        user_doc = await self._user_repo.find_by_id(user_id)
        if not user_doc:
            return None

        profile: dict = {
            "id": str(user_doc.id),
            "email": user_doc.email,
            "email_verified": user_doc.email_verified,
            "user_name": user_doc.user_name,
            "password_set": user_doc.password_set,
            "auth_providers": [
                {
                    "provider": p.provider,
                    "email": p.email,
                    "linked_at": p.linked_at.isoformat() if p.linked_at else None,
                }
                for p in (user_doc.auth_providers or [])
            ],
        }

        if user_doc.pfp:
            profile["pfp"] = {"url": user_doc.pfp.url, "source": user_doc.pfp.source}

        return profile

    async def update_user_name(
        self, user_id: ObjectId, user_name: str | None
    ) -> UserDoc:
        """Set (or clear, with None) the user's display name.

        Returns the updated user document for the profile response.
        Raises NotFoundError if the user is not found.
        """
        user_doc = await self._user_repo.find_by_id(user_id)
        if not user_doc:
            raise NotFoundError("User not found")

        await self._user_repo.update(user_id, {"$set": {"user_name": user_name}})
        log.info(
            "user_name_updated",
            user_id=str(user_id),
            cleared=user_name is None,
        )
        return user_doc.model_copy(update={"user_name": user_name})

    async def get_available_pictures(self, user_id: ObjectId) -> list[AvailablePicture]:
        """Return profile pictures available from connected OAuth providers.

        Raises NotFoundError if the user is not found.
        """
        user_doc = await self._user_repo.find_by_id(user_id)
        if not user_doc:
            raise NotFoundError("User not found")

        current_pfp_url = user_doc.pfp.url if user_doc.pfp else None
        pictures = []
        for provider in user_doc.auth_providers or []:
            picture_url = provider.profile.picture if provider.profile else None
            if picture_url:
                pictures.append(
                    AvailablePicture(
                        id=f"{provider.provider}_{provider.provider_user_id}",
                        url=picture_url,
                        source=provider.provider,
                        is_current=current_pfp_url == picture_url,
                    )
                )
        return pictures

    async def set_picture(self, user_id: ObjectId, picture_id: str) -> None:
        """Set the user's profile picture from an OAuth provider.

        Only allows pictures that exist in the user's auth_providers array.
        Raises NotFoundError if user or picture_id is not found.
        """
        user_doc = await self._user_repo.find_by_id(user_id)
        if not user_doc:
            raise NotFoundError("User not found")

        for provider in user_doc.auth_providers or []:
            provider_id = f"{provider.provider}_{provider.provider_user_id}"
            if provider_id == picture_id:
                picture_url = provider.profile.picture if provider.profile else None
                if picture_url:
                    pfp = ProfilePicture(
                        url=picture_url,
                        source=provider.provider,
                        last_updated=datetime.now(timezone.utc),
                    )
                    await self._user_repo.update(
                        user_id,
                        {"$set": {"pfp": pfp.model_dump()}},
                    )
                    log.info(
                        "profile_picture_updated",
                        user_id=str(user_id),
                        source=provider.provider,
                    )
                    return

        raise NotFoundError("Picture not found")

    async def upload_picture(self, user_id: ObjectId, image: str) -> None:
        """Set the profile picture from an uploaded base64 data URI.

        Same gates as meta-tag og:image uploads (decode_image_data_uri),
        stored content-addressed under profile-pictures/{owner-prefix}/.
        Raises NotFoundError if the user is not found, ValidationError on
        a bad payload or when R2 is not configured.
        """
        user_doc = await self._user_repo.find_by_id(user_id)
        if not user_doc:
            raise NotFoundError("User not found")

        parts = split_image_data_uri(image)
        if parts is None:
            raise ValidationError(
                "image must be a base64 data URI (image/png, image/jpeg, image/webp)",
                field="image",
            )
        if self._r2_storage is None or not self._r2_storage.is_configured:
            raise ValidationError(
                "Image upload is not available on this deployment",
                field="image",
            )
        fmt, b64 = parts
        decoded = decode_image_data_uri(
            fmt, b64, max_bytes=self._upload_max_bytes, field="image"
        )

        # Content-addressed + owner-scoped, mirroring og/ keys: re-uploads
        # dedupe, takedowns can prefix-sweep profile-pictures/{prefix}/.
        # Replaced objects are not deleted (same orphan-GC stance as og/).
        prefix = user_doc.storage_prefix
        if prefix is None:
            prefix = owner_key_prefix(user_id, self._key_secret)
            # Pin the prefix on first upload — a SECRET_KEY rotation would
            # otherwise re-key it and orphan the objects for erasure sweeps.
            await self._user_repo.set_storage_prefix_if_absent(user_id, prefix)
        digest = hashlib.sha256(decoded.data).hexdigest()
        key = f"profile-pictures/{prefix}/{digest}.{EXT[decoded.info.format]}"
        url = await self._r2_storage.put_object(
            key, decoded.data, content_type=decoded.content_type
        )

        pfp = ProfilePicture(
            url=url,
            source="upload",
            last_updated=datetime.now(timezone.utc),
        )
        await self._user_repo.update(user_id, {"$set": {"pfp": pfp.model_dump()}})
        log.info(
            "profile_picture_uploaded",
            user_id=str(user_id),
            bytes=len(decoded.data),
            format=decoded.info.format,
        )

    async def unset_picture(self, user_id: ObjectId) -> None:
        """Clear the profile picture back to none (initials avatar).

        Idempotent. Raises NotFoundError if the user is not found.
        """
        user_doc = await self._user_repo.find_by_id(user_id)
        if not user_doc:
            raise NotFoundError("User not found")

        await self._user_repo.update(user_id, {"$set": {"pfp": None}})
        log.info("profile_picture_unset", user_id=str(user_id))
