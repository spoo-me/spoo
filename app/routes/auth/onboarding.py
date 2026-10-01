"""
Onboarding routes — resume pointer + completion fact, kept separate.

GET  /auth/onboarding          → stored resume pointer (empty when unset/expired)
PUT  /auth/onboarding          → persist the pointer (Redis, 24h TTL)
POST /auth/onboarding/complete → stamp UserDoc.onboarded_at, drop the pointer

The pointer is intentionally soft (OnboardingCache: TTL, None-redis
tolerant, errors swallowed) — the frontend falls back to localStorage.
Completion is the opposite: a permanent account fact on the user
document, exposed to clients as ``user.onboarded_at`` via /auth/me.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request

from app.dependencies import AuthUser, OnboardingCacheDep, UserRepo
from app.infrastructure.logging import get_logger
from app.middleware.openapi import ERROR_RESPONSES
from app.middleware.rate_limiter import Limits, limiter
from app.schemas.dto.requests.auth import (
    OnboardingCompleteRequest,
    OnboardingStateRequest,
)
from app.schemas.dto.responses.auth import (
    OnboardingCompleteResponse,
    OnboardingStateResponse,
)

log = get_logger(__name__)

router = APIRouter()


@router.get(
    "/auth/onboarding",
    responses=ERROR_RESPONSES,
    operation_id="getOnboardingState",
    summary="Get Onboarding State",
)
@limiter.limit(Limits.AUTH_READ)
async def get_onboarding_state(
    request: Request,
    user: AuthUser,
    cache: OnboardingCacheDep,
) -> OnboardingStateResponse:
    """Return the caller's onboarding resume pointer.

    **Authentication**: Required (JWT or API key)

    **Rate Limits**: 60/min

    **Notes**: The pointer self-expires 24h after the last write. Empty
    (`step: null`) means nothing to resume — never started, expired, or
    completed. Whether the account has *finished* onboarding is
    `user.onboarded_at` on `/auth/me`, not part of this cache.
    """
    data = await cache.get(str(user.user_id)) or {}
    return OnboardingStateResponse(step=data.get("step"), path=data.get("path"))


@router.put(
    "/auth/onboarding",
    responses=ERROR_RESPONSES,
    operation_id="putOnboardingState",
    summary="Update Onboarding State",
)
@limiter.limit(Limits.ONBOARDING_WRITE)
async def put_onboarding_state(
    request: Request,
    body: OnboardingStateRequest,
    user: AuthUser,
    cache: OnboardingCacheDep,
) -> OnboardingStateResponse:
    """Persist the caller's resume pointer (refreshes the 24h TTL).

    **Authentication**: Required (JWT or API key)

    **Rate Limits**: 30/min

    **Notes**: Best-effort — if Redis is unavailable the response echoes
    the submitted state without persisting it; clients keep their local
    copy as the fallback source of truth.
    """
    await cache.set(str(user.user_id), body.step, body.path)
    return OnboardingStateResponse(step=body.step, path=body.path)


@router.post(
    "/auth/onboarding/complete",
    responses=ERROR_RESPONSES,
    operation_id="completeOnboarding",
    summary="Complete Onboarding",
)
@limiter.limit(Limits.ONBOARDING_WRITE)
async def complete_onboarding(
    request: Request,
    body: OnboardingCompleteRequest,
    user: AuthUser,
    user_repo: UserRepo,
    cache: OnboardingCacheDep,
) -> OnboardingCompleteResponse:
    """Mark onboarding finished for this account.

    Stamps `onboarded_at` on the user document (first completion wins —
    repeat calls are idempotent and keep the original timestamp), records
    the optional HDYHAU answer, and drops the resume pointer.

    **Authentication**: Required (JWT or API key)

    **Rate Limits**: 30/min
    """
    now = datetime.now(timezone.utc)
    stamped = await user_repo.complete_onboarding(user.user_id, now, body.heard_from)
    await cache.delete(str(user.user_id))
    if stamped:
        log.info("onboarding_completed", user_id=str(user.user_id))
        return OnboardingCompleteResponse(success=True, onboarded_at=now)
    # Already completed earlier — idempotent; echo the original stamp.
    existing = await user_repo.find_by_id(user.user_id)
    when = (
        existing.onboarded_at
        if existing is not None and existing.onboarded_at is not None
        else now
    )
    return OnboardingCompleteResponse(success=True, onboarded_at=when)
