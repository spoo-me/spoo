"""
Onboarding state routes — Dub-style self-expiring step cache.

GET /auth/onboarding → stored wizard progress (empty when unset or expired)
PUT /auth/onboarding → persist wizard progress (Redis, 24h TTL)

The cache is intentionally soft: when the TTL lapses or Redis is down the
state simply reads as empty and the frontend falls back to localStorage.
No user-document schema change required.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from dependencies import AuthUser, get_redis
from infrastructure.logging import get_logger
from middleware.openapi import ERROR_RESPONSES
from middleware.rate_limiter import Limits, limiter
from schemas.dto.requests.auth import OnboardingStateRequest
from schemas.dto.responses.auth import OnboardingStateResponse

log = get_logger(__name__)

router = APIRouter()

RedisDep = Annotated[Any, Depends(get_redis)]

ONBOARDING_TTL_SECONDS = 24 * 60 * 60  # Dub's ONBOARDING_WINDOW: soft 24h gate


def _key(user_id: str) -> str:
    return f"onboarding:{user_id}"


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
    redis: RedisDep,
) -> OnboardingStateResponse:
    """Return the caller's stored onboarding progress.

    **Authentication**: Required (JWT or API key)

    **Rate Limits**: 60/min

    **Notes**: Progress self-expires 24h after the last write. An empty
    response (`step: null`) means "never started or expired" — clients
    should treat expiry as the gate being lifted, not as a reset trap.
    """
    if redis is None:
        return OnboardingStateResponse()
    try:
        raw = await redis.get(_key(str(user.user_id)))
    except Exception as exc:
        log.warning("onboarding_state_read_failed", error=str(exc))
        return OnboardingStateResponse()
    if not raw:
        return OnboardingStateResponse()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return OnboardingStateResponse()
    step = data.get("step")
    return OnboardingStateResponse(
        step=step,
        path=data.get("path"),
        completed=step == "completed",
    )


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
    redis: RedisDep,
) -> OnboardingStateResponse:
    """Persist the caller's onboarding progress (refreshes the 24h TTL).

    **Authentication**: Required (JWT or API key)

    **Rate Limits**: 30/min

    **Notes**: Best-effort — if Redis is unavailable the response echoes
    the submitted state without persisting it; clients keep their local
    copy as the fallback source of truth.
    """
    state = OnboardingStateResponse(
        step=body.step,
        path=body.path,
        completed=body.step == "completed",
    )
    if redis is None:
        return state
    try:
        await redis.set(
            _key(str(user.user_id)),
            json.dumps({"step": body.step, "path": body.path}),
            ex=ONBOARDING_TTL_SECONDS,
        )
    except Exception as exc:
        log.warning("onboarding_state_write_failed", error=str(exc))
    return state
