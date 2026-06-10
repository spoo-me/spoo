"""
OAuth routes — /oauth/*

GET    /oauth/providers                           — list linked providers
DELETE /oauth/providers/{provider_name}/unlink    — unlink a provider
GET    /oauth/{provider}                          — initiate OAuth login
GET    /oauth/{provider}/callback                 — handle OAuth callback
GET    /oauth/{provider}/link                     — link provider to existing account

NOTE: The two fixed-prefix paths (/oauth/providers, /oauth/providers/*/unlink)
are registered BEFORE the parametric /{provider} routes to prevent the path
parameter from capturing the literal "providers" segment.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse

from dependencies import AuthUser, JwtConfig, OAuthProviders, OAuthSvc, Settings
from errors import AppError, NotFoundError, ValidationError
from infrastructure.logging import get_logger
from infrastructure.oauth_clients import (
    PROVIDER_STRATEGIES,
    generate_oauth_state,
    get_oauth_redirect_url,
    verify_oauth_state,
)
from middleware.openapi import ERROR_RESPONSES, PUBLIC_SECURITY
from middleware.rate_limiter import Limits, limiter
from routes.cookie_helpers import set_auth_cookies
from schemas.dto.responses.auth import OAuthProvidersResponse
from schemas.dto.responses.common import MessageResponse
from schemas.models.user import OAuthAction
from shared.ip_utils import get_client_ip
from shared.validators import validate_safe_redirect

log = get_logger(__name__)

router = APIRouter(prefix="/oauth", tags=["OAuth"])

_DASHBOARD_URL = "/dashboard"


# ── Provider management (fixed paths — must come before parametric routes) ────


@router.get(
    "/providers",
    responses=ERROR_RESPONSES,
    operation_id="listOAuthProviders",
    summary="List OAuth Providers",
)
@limiter.limit(Limits.AUTH_READ)
async def list_providers(
    request: Request,
    user: AuthUser,
    oauth_service: OAuthSvc,
) -> OAuthProvidersResponse:
    """List all OAuth providers linked to the authenticated user's account.

    Returns each linked provider's name, email, and link date, plus whether
    the user has a password set (needed by the UI to decide if unlinking
    the last provider is allowed).

    **Authentication**: Required (JWT or API key)

    **Rate Limits**: 60/min
    """
    providers, password_set = await oauth_service.list_providers(str(user.user_id))
    return OAuthProvidersResponse(providers=providers, password_set=password_set)


@router.delete(
    "/providers/{provider_name}/unlink",
    responses=ERROR_RESPONSES,
    operation_id="unlinkOAuthProvider",
    summary="Unlink OAuth Provider",
)
@limiter.limit(Limits.OAUTH_DISCONNECT)
async def unlink_provider(
    provider_name: str,
    request: Request,
    user: AuthUser,
    oauth_service: OAuthSvc,
) -> MessageResponse:
    """Remove an OAuth provider link from the authenticated user's account.

    Fails if the provider is the user's only authentication method (i.e.,
    no password set and no other providers linked).

    **Authentication**: Required (JWT or API key)

    **Rate Limits**: 5/min
    """
    await oauth_service.unlink_provider(str(user.user_id), provider_name)
    return MessageResponse(
        success=True, message=f"{provider_name} unlinked successfully"
    )


# ── OAuth flow (parametric provider routes — after fixed paths) ───────────────


@router.get(
    "/{provider}",
    responses=ERROR_RESPONSES,
    openapi_extra=PUBLIC_SECURITY,
    operation_id="initiateOAuthLogin",
    summary="OAuth Login",
)
@limiter.limit(Limits.OAUTH_INIT)
async def oauth_login(
    provider: str,
    request: Request,
    oauth_providers: OAuthProviders,
    settings: Settings,
) -> Response:
    """Initiate the OAuth authorization flow for the given provider.

    Redirects the user to the provider's consent screen (e.g., Google,
    GitHub). After the user grants access, the provider redirects back
    to ``GET /oauth/{provider}/callback``.

    **Authentication**: Not required (public endpoint)

    **Rate Limits**: 10/min

    **Supported providers**: google, github (configurable)
    """
    strategy = PROVIDER_STRATEGIES.get(provider)
    client = oauth_providers.get(provider)
    if not strategy or not client:
        raise NotFoundError(f"'{provider}' OAuth not configured")

    log.info("oauth_flow_initiated", provider=provider)
    next_url = request.query_params.get("next")
    state = generate_oauth_state(provider, OAuthAction.LOGIN, next_url=next_url)
    redirect_uri = get_oauth_redirect_url(provider, settings.oauth)
    return await client.authorize_redirect(request, redirect_uri, state=state)


@router.get(
    "/{provider}/callback",
    responses=ERROR_RESPONSES,
    openapi_extra=PUBLIC_SECURITY,
    operation_id="oauthCallback",
    summary="OAuth Callback",
)
@limiter.limit(Limits.OAUTH_CALLBACK)
async def oauth_callback(
    provider: str,
    request: Request,
    oauth_service: OAuthSvc,
    oauth_providers: OAuthProviders,
    jwt_cfg: JwtConfig,
) -> Response:
    """Handle the OAuth provider callback after user authorization.

    Validates the CSRF state parameter, exchanges the authorization code for
    an access token, fetches the user's profile from the provider, and then
    either logs in an existing user or creates a new account. On success,
    redirects to ``/dashboard`` with JWT cookies set.

    **Authentication**: Not required (public endpoint)

    **Rate Limits**: 20/min

    **Notes**: This endpoint is called by the OAuth provider, not directly
    by the client. The ``state`` query parameter is required for CSRF protection.
    """
    strategy = PROVIDER_STRATEGIES.get(provider)
    client = oauth_providers.get(provider)
    if not strategy or not client:
        raise NotFoundError(f"'{provider}' OAuth not configured")

    # ── CSRF / state validation ───────────────────────────────────────────────
    state = request.query_params.get("state")
    if not state:
        raise ValidationError("missing state parameter")

    is_valid, state_data, failure_reason = verify_oauth_state(state, provider)
    if not is_valid:
        log.warning("oauth_state_invalid", provider=provider, reason=failure_reason)
        raise ValidationError("invalid state parameter")

    # ── Provider-reported error ───────────────────────────────────────────────
    error = request.query_params.get("error")
    if error:
        error_description = request.query_params.get(
            "error_description", "OAuth authorization failed"
        )
        raise ValidationError(f"OAuth error: {error_description}")

    # ── Token exchange + provider user-info ──────────────────────────────────
    try:
        token = await client.authorize_access_token(request)
        provider_info = await strategy.fetch_user_info(client, token)
    except Exception as exc:
        log.error(
            "oauth_callback_failed",
            provider=provider,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        raise AppError("OAuth authentication failed") from exc

    # ── Delegate to service ───────────────────────────────────────────────────
    action = state_data.get("action", OAuthAction.LOGIN)
    client_ip = get_client_ip(request)
    result = await oauth_service.handle_callback(
        provider, provider_info, action, state_data, client_ip
    )

    # ── Redirect with cookies ──────────────────────────────────────────────
    # Brand-new accounts go to onboarding unless the flow carried an explicit
    # destination (e.g. device-consent deep links must not be hijacked).
    if result.is_new and not state_data.get("next"):
        next_url = "/onboarding"
    else:
        next_url = validate_safe_redirect(state_data.get("next", ""))
    resp = RedirectResponse(next_url, status_code=302)
    set_auth_cookies(resp, result.access_token, result.refresh_token, jwt_cfg)
    return resp


@router.get(
    "/{provider}/link",
    responses=ERROR_RESPONSES,
    operation_id="linkOAuthProvider",
    summary="Link OAuth Provider",
)
@limiter.limit(Limits.OAUTH_LINK)
async def oauth_link(
    provider: str,
    request: Request,
    user: AuthUser,
    oauth_providers: OAuthProviders,
    settings: Settings,
) -> Response:
    """Initiate an OAuth flow to link a provider to the authenticated account.

    Similar to ``GET /oauth/{provider}`` but includes the user's ID in the
    state token so the callback knows to link rather than log in. The user
    must already be authenticated.

    **Authentication**: Required (JWT or API key)

    **Rate Limits**: 5/min
    """
    strategy = PROVIDER_STRATEGIES.get(provider)
    client = oauth_providers.get(provider)
    if not strategy or not client:
        raise NotFoundError(f"'{provider}' OAuth not configured")

    log.info("oauth_link_initiated", provider=provider, user_id=str(user.user_id))
    state = generate_oauth_state(provider, OAuthAction.LINK, user_id=str(user.user_id))
    redirect_uri = get_oauth_redirect_url(provider, settings.oauth)
    return await client.authorize_redirect(request, redirect_uri, state=state)
