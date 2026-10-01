"""
GET /api/v1/apps — list the caller's connected apps (active device grants)

JWT-only, matching /api/v1/keys: grants are an account-security surface,
and an API key must not be able to enumerate the account's other delegated
credentials (or their revoke handles).
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.dependencies import AppGrantRepo, AppRegistryDep, JwtUser
from app.middleware.openapi import AUTH_RESPONSES
from app.middleware.rate_limiter import Limits, limiter
from app.schemas.dto.responses.app_grant import AppGrantResponse, AppGrantsListResponse
from app.services.auth.device import effective_scopes_for

router = APIRouter(tags=["Apps"])


@router.get(
    "/apps",
    responses=AUTH_RESPONSES,
    operation_id="listAppGrants",
    summary="List Connected Apps",
)
@limiter.limit(Limits.APP_GRANTS_READ)
async def list_app_grants(
    request: Request,
    user: JwtUser,
    grant_repo: AppGrantRepo,
    app_registry: AppRegistryDep,
) -> AppGrantsListResponse:
    """List the apps connected to the authenticated user's account.

    Returns one entry per active device-auth grant (revoked grants are
    excluded), newest first. `app` is the registry key from
    `config/apps.yaml` — pass it as `app_id` to `POST /auth/device/revoke`
    to disconnect an app. An empty `items` array means nothing is
    connected.

    **Authentication**: Required — JWT Bearer only (API keys cannot list
    the account's connected apps).

    **Rate Limits**: 60/min
    """
    grants = await grant_repo.find_active_for_user(user.user_id)
    grants.sort(key=lambda g: g.granted_at, reverse=True)
    return AppGrantsListResponse(
        items=[
            AppGrantResponse.from_grant(
                g,
                app_registry.get(g.app_id),
                effective_scopes_for(g, app_registry.get(g.app_id)),
            )
            for g in grants
        ]
    )
