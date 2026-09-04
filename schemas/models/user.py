"""
User document model.

Maps to the `users` MongoDB collection.

Two creation paths produce slightly different shapes:
- Password registration: no last_login_at, pfp is None
- OAuth registration: last_login_at set, pfp may be populated

Both paths are handled via Optional fields with sensible defaults.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel

from schemas.models.base import MongoBaseModel


class UserStatus(str, Enum):
    """Status values for user accounts."""

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    PENDING_DELETION = "PENDING_DELETION"
    # Claimed by the erasure cascade — the point of no return: restore only
    # matches PENDING_DELETION, so it can't resurrect a half-erased account.
    ERASING = "ERASING"


class OAuthAction(str, Enum):
    """OAuth flow action types."""

    LOGIN = "login"
    LINK = "link"


class OAuthProvider(str, Enum):
    """Supported OAuth providers."""

    GOOGLE = "google"
    GITHUB = "github"
    DISCORD = "discord"


class ProviderInfo(BaseModel):
    """Normalised user-info returned by OAuth provider strategies.

    All three providers (Google, GitHub, Discord) produce the same shape.
    Pydantic will coerce a plain dict into this model automatically.
    """

    provider_user_id: str
    email: str
    email_verified: bool = False
    name: str | None = None
    picture: str | None = None
    given_name: str | None = None
    family_name: str | None = None


class ProviderProfile(BaseModel):
    """Nested profile data stored per OAuth provider."""

    name: str | None = None
    picture: str | None = None


class AuthProviderEntry(BaseModel):
    """Single entry in the user's auth_providers array."""

    provider: OAuthProvider
    provider_user_id: str
    email: str | None = None
    email_verified: bool = False
    profile: ProviderProfile = ProviderProfile()
    linked_at: datetime | None = None


class ProfilePicture(BaseModel):
    """Embedded profile picture sub-document.

    ``source`` is the OAuth provider the picture came from, or ``upload``
    for a user-uploaded image (stored in R2).
    """

    url: str
    source: OAuthProvider | Literal["upload"]
    last_updated: datetime | None = None


class UserDoc(MongoBaseModel):
    """
    Document model for the `users` collection.

    status: UserStatus enum (ACTIVE, INACTIVE, PENDING_DELETION)
    """

    email: str
    email_verified: bool = False
    password_hash: str | None = None
    password_set: bool = False
    user_name: str | None = None
    pfp: ProfilePicture | None = None
    auth_providers: list[AuthProviderEntry] = []  # noqa: RUF012
    signup_ip: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_login_at: datetime | None = None
    # Permanent completion fact — the onboarding wizard's resume pointer is
    # ephemeral (Redis, 24h), but "this account finished onboarding" must
    # survive it. Null = never finished.
    onboarded_at: datetime | None = None
    # HDYHAU attribution, captured once at onboarding completion.
    heard_from: str | None = None
    status: UserStatus = UserStatus.ACTIVE
    # Account deletion (GDPR erasure): both set on PENDING_DELETION, both
    # cleared on restore. Null on every account that never requested deletion.
    deletion_requested_at: datetime | None = None
    purge_after: datetime | None = None
    # Stamped by every erasure claim (never set outside ERASING): older than
    # the lease = crashed cascade (re-claimable), fresh = live (untouchable).
    erasure_claimed_at: datetime | None = None
    # R2 owner-key prefix, pinned on first upload so SECRET_KEY rotations
    # can't hide the objects from erasure sweeps. Null until first upload.
    storage_prefix: str | None = None
