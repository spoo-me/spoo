"""
POST   /api/v1/keys          — create an API key
GET    /api/v1/keys          — list API keys for the current user
DELETE /api/v1/keys/{key_id} — delete (?revoke=false) or revoke (?revoke=true) a key

All endpoints require an interactive session (JWT without app scopes) OR a
device-app token whose grant holds `keys:manage` (e.g. spoo-cli). API keys
may NOT be used to manage other API keys — keys:manage is excluded from
ALLOWED_SCOPES at creation, so a key can never propagate itself.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from bson import ObjectId
from fastapi import APIRouter, Path, Query, Request

from dependencies import (
    ApiKeySvc,
    Entitled,
    JwtVerifiedUser,
    KeysAccessUser,
)
from errors import NotFoundError, ValidationError
from middleware.openapi import AUTH_RESPONSES, ERROR_RESPONSES
from middleware.rate_limiter import Limits, limiter
from schemas.dto.requests.api_key import CreateApiKeyRequest
from schemas.dto.responses.api_key import (
    ApiKeyActionResponse,
    ApiKeyCreatedResponse,
    ApiKeyResponse,
    ApiKeysListResponse,
)
from shared.datetime_utils import parse_datetime, to_unix_timestamp

router = APIRouter(tags=["API Keys"])


@router.post(
    "/keys",
    status_code=201,
    responses=AUTH_RESPONSES,
    operation_id="createApiKey",
    summary="Create API Key",
)
@limiter.limit(Limits.API_KEY_CREATE)
async def create_api_key(
    request: Request,
    body: CreateApiKeyRequest,
    user: JwtVerifiedUser,
    api_key_service: ApiKeySvc,
    entitlements: Entitled,
) -> ApiKeyCreatedResponse:
    """Create a new API key for programmatic access.

    Generate a new API key with the specified name and scopes. The full token
    (prefixed with `spoo_`) is returned **only in this response** and cannot be
    retrieved again.

    **Authentication**: Required — interactive session only. Connected apps
    and API keys cannot create keys; minting a credential is a first-party
    action. Email must be verified.

    **Rate Limits**: 5/hour

    **Available Scopes**: `shorten:create`, `urls:manage`, `urls:read`,
    `stats:read`, `domains:manage`, `domains:read`, `reports:create`,
    `admin:all`

    **Notes**:

    - Store the returned `token` securely — it will not be shown again
    - Set `expires_at` to limit the key's lifetime (ISO 8601 or Unix epoch)
    - Omit `expires_at` for a non-expiring key
    """
    # Parse expires_at from the raw value in the DTO
    expires_at: datetime | None = None
    if body.expires_at is not None:
        expires_at = parse_datetime(body.expires_at)
        if expires_at is None:
            raise ValidationError(
                "expires_at must be ISO 8601 or Unix epoch seconds",
                field="expires_at",
            )
        if expires_at <= datetime.now(timezone.utc):
            raise ValidationError(
                "expires_at must be in the future", field="expires_at"
            )

    key_doc, raw_token = await api_key_service.create(
        name=body.name,
        scopes=body.scopes,
        user_id=user.user_id,
        email_verified=user.email_verified,
        description=body.description,
        expires_at=expires_at,
        max_keys=entitlements.limit("api_keys_max"),
    )

    return ApiKeyCreatedResponse(
        id=str(key_doc.id),
        name=key_doc.name,
        description=key_doc.description,
        scopes=key_doc.scopes,
        created_at=to_unix_timestamp(key_doc.created_at),
        expires_at=to_unix_timestamp(key_doc.expires_at),
        revoked=key_doc.revoked,
        token_prefix=key_doc.token_prefix,
        token=raw_token,
    )


@router.get(
    "/keys",
    responses=ERROR_RESPONSES,
    operation_id="listApiKeys",
    summary="List API Keys",
)
@limiter.limit(Limits.API_KEY_READ)
async def list_api_keys(
    request: Request,
    user: KeysAccessUser,
    api_key_service: ApiKeySvc,
) -> ApiKeysListResponse:
    """List all API keys for the authenticated user.

    Returns metadata for all API keys (both active and revoked) belonging to the
    authenticated user. The full token value is **never** returned in this
    endpoint for security reasons — only the `token_prefix` is shown.

    **Authentication**: Required — interactive session or an app token with
    the `keys:manage` scope (API keys cannot be used to manage API keys).

    **Rate Limits**: 60/min
    """
    keys = await api_key_service.list_by_user(user.user_id)
    return ApiKeysListResponse(
        keys=[
            ApiKeyResponse(
                id=str(k.id),
                name=k.name,
                description=k.description,
                scopes=k.scopes,
                created_at=to_unix_timestamp(k.created_at),
                expires_at=to_unix_timestamp(k.expires_at),
                revoked=k.revoked,
                token_prefix=k.token_prefix,
                last_used_at=to_unix_timestamp(k.last_used_at),
            )
            for k in keys
        ]
    )


@router.delete(
    "/keys/{key_id}",
    responses=ERROR_RESPONSES,
    operation_id="deleteApiKey",
    summary="Delete/Revoke API Key",
)
@limiter.limit(Limits.API_KEY_DELETE)
async def delete_api_key(
    request: Request,
    key_id: Annotated[
        str, Path(min_length=24, max_length=24, pattern=r"^[0-9a-f]{24}$")
    ],
    user: KeysAccessUser,
    api_key_service: ApiKeySvc,
    revoke: bool = Query(default=False),
) -> ApiKeyActionResponse:
    """Delete or revoke an API key.

    Remove an API key either permanently (hard delete) or by marking it as
    revoked (soft delete). Revoked keys stop working immediately but remain
    visible in the key list for audit purposes.

    **Authentication**: Required — interactive session or an app token with
    the `keys:manage` scope (API keys cannot be used to manage API keys).

    **Rate Limits**: 30/min

    **Modes**:

    - `?revoke=false` (default) — **permanently deletes** the key record
    - `?revoke=true` — marks the key as revoked but preserves the record;
      the key appears with `revoked: true` in the list endpoint
    """
    try:
        key_oid = ObjectId(key_id)
    except Exception:
        raise NotFoundError("key not found or access denied") from None

    ok = await api_key_service.revoke(user.user_id, key_oid, hard_delete=not revoke)
    if not ok:
        raise NotFoundError("key not found or access denied")

    return ApiKeyActionResponse(success=True, action="revoked" if revoke else "deleted")
