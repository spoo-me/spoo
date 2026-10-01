"""
API key document model.

Maps to the `api-keys` MongoDB collection.

token_hash stores SHA-256(raw_token) — the raw token is shown once at creation
and never stored. token_prefix (first 8 chars) is stored for display purposes.
"""

from __future__ import annotations

from datetime import datetime

from schemas.dto.requests.api_key import ApiKeyScope
from schemas.models.base import MongoBaseModel, PyObjectId


class ApiKeyDoc(MongoBaseModel):
    """Document model for the `api-keys` collection."""

    user_id: PyObjectId
    token_prefix: str
    token_hash: str
    name: str
    description: str | None = None
    scopes: list[ApiKeyScope] = []  # noqa: RUF012
    expires_at: datetime | None = None
    created_at: datetime | None = None
    revoked: bool = False
    # Newest keys beyond the plan's limit stop authenticating until it grows.
    paused_by_limit: bool = False
    # Stamped on successful auth, debounced to at most one write per hour —
    # accurate enough for "is this key still in use", cheap on the hot path.
    last_used_at: datetime | None = None
