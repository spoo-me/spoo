"""
Device auth routes — browser extensions, CLIs, desktop apps.

GET  /auth/device/login      → device auth initiation + consent
POST /auth/device/consent    → consent form submission
GET  /auth/device/callback   → code delivery page for extensions
POST /auth/device/token      → exchange code for JWT tokens
POST /auth/device/refresh    → refresh app tokens (body-based)
POST /auth/device/revoke     → revoke app access
"""

from __future__ import annotations

import secrets
from urllib.parse import quote, urlencode

from bson import ObjectId
from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from dependencies import (
    AppGrantRepo,
    CredentialSvc,
    DeviceAuthSvc,
    JwtConfig,
    JwtUser,
    OptionalUser,
    UserRepo,
    fetch_user_profile,
)
from errors import AuthenticationError
from infrastructure.logging import get_logger
from infrastructure.templates import templates
from middleware.openapi import ERROR_RESPONSES, PUBLIC_SECURITY
from middleware.rate_limiter import Limits, limiter
from schemas.dto.requests.auth import DeviceRefreshRequest, DeviceTokenRequest
from schemas.dto.responses.auth import (
    DeviceRefreshResponse,
    DeviceTokenResponse,
    UserProfileResponse,
)
from schemas.models.app import AppEntry
from services.auth.device import APP_ID_MAX_LEN
from shared.generators import generate_secure_token

log = get_logger(__name__)

router = APIRouter()

# ── Constants (CSRF is a route-layer concern) ────────────────────────────────

_CSRF_COOKIE_NAME = "_consent_csrf"
_CSRF_TTL_SECONDS = 600
_CSRF_TOKEN_BYTES = 32
_CSRF_HEADER_NAME = "x-requested-with"
_CSRF_HEADER_VALUE = "fetch"


# ── Response builders ────────────────────────────────────────────────────────


def _device_error(request: Request, error: str, status_code: int = 400) -> Response:
    """Render the device auth error page."""
    return templates.TemplateResponse(
        request, "device_error.html", {"error": error}, status_code=status_code
    )


def _build_callback_redirect(
    code: str,
    state: str,
    redirect_uri: str,
    app: AppEntry,
    svc: DeviceAuthSvc,
) -> RedirectResponse:
    """Build the redirect to the callback page or a registered redirect_uri."""
    params = urlencode({"code": code, "state": state})
    if redirect_uri and svc.validate_redirect_uri(redirect_uri, app):
        separator = "&" if "?" in redirect_uri else "?"
        return RedirectResponse(f"{redirect_uri}{separator}{params}", status_code=302)
    return RedirectResponse(f"/auth/device/callback?{params}", status_code=302)


# ── Routes ───────────────────────────────────────────────────────────────────


@router.get("/auth/device/login", include_in_schema=False)
@limiter.limit(Limits.DEVICE_AUTH)
async def device_login(
    request: Request,
    user: OptionalUser,
    device_auth_service: DeviceAuthSvc,
    user_repo: UserRepo,
    grant_repo: AppGrantRepo,
    jwt_cfg: JwtConfig,
    app_id: str = "",
    redirect_uri: str = "",
    state: str = "",
) -> Response:
    """Initiate the device auth flow with app identification and consent.

    Validates the app_id against the registry. If the user has an existing
    active grant, auto-approves and generates a code. Otherwise shows the
    consent screen.
    """
    app = device_auth_service.resolve_app(app_id)
    if not app:
        return _device_error(request, "Unknown or unsupported application")

    if not device_auth_service.validate_redirect_uri(redirect_uri, app):
        return _device_error(request, "Invalid redirect URI for this application")

    if not user:
        params: dict[str, str] = {"app_id": app_id}
        if state:
            params["state"] = state
        if redirect_uri:
            params["redirect_uri"] = redirect_uri
        next_url = f"/auth/device/login?{urlencode(params)}"
        return RedirectResponse(f"/?next={quote(next_url)}", status_code=302)

    # Check for existing active grant (auto-approve)
    grant = await grant_repo.find_active_grant(user.user_id, app_id)
    if grant:
        profile = await fetch_user_profile(user_repo, ObjectId(str(user.user_id)))
        code = await device_auth_service.create_device_auth_code(
            profile.id, profile.email, app_id=app_id
        )
        return _build_callback_redirect(
            code, state, redirect_uri, app, device_auth_service
        )

    # No grant: show consent screen
    csrf_token = generate_secure_token(_CSRF_TOKEN_BYTES)
    profile = await fetch_user_profile(user_repo, ObjectId(str(user.user_id)))
    response = templates.TemplateResponse(
        request,
        "device_consent.html",
        {
            "app": app,
            "app_id": app_id,
            "state": state,
            "redirect_uri": redirect_uri,
            "csrf_token": csrf_token,
            "user": profile,
        },
    )
    response.set_cookie(
        _CSRF_COOKIE_NAME,
        csrf_token,
        httponly=True,
        secure=jwt_cfg.cookie_secure,
        samesite="strict",
        max_age=_CSRF_TTL_SECONDS,
    )
    return response


@router.post("/auth/device/consent", include_in_schema=False)
@limiter.limit(Limits.DEVICE_AUTH)
async def device_consent_approve(
    request: Request,
    user: JwtUser,
    device_auth_service: DeviceAuthSvc,
    user_repo: UserRepo,
    grant_repo: AppGrantRepo,
    app_id: str = Form(""),
    state: str = Form(""),
    csrf_token: str = Form(""),
    redirect_uri: str = Form(""),
) -> Response:
    """Handle consent form submission (Allow button)."""
    # CSRF validation
    cookie_csrf = request.cookies.get(_CSRF_COOKIE_NAME)
    if (
        not cookie_csrf
        or not csrf_token
        or not secrets.compare_digest(csrf_token, cookie_csrf)
    ):
        return _device_error(
            request, "Invalid or expired consent session. Please try again.", 403
        )

    app = device_auth_service.resolve_app(app_id)
    if not app:
        return _device_error(request, "Unknown or unsupported application")

    if not device_auth_service.validate_redirect_uri(redirect_uri, app):
        return _device_error(request, "Invalid redirect URI for this application")

    # Create grant
    await grant_repo.create_or_reactivate(user.user_id, app_id)
    log.info("app_consent_granted", user_id=str(user.user_id), app_id=app_id)

    # Generate device auth code
    profile = await fetch_user_profile(user_repo, ObjectId(str(user.user_id)))
    code = await device_auth_service.create_device_auth_code(
        profile.id, profile.email, app_id=app_id
    )

    # Clear CSRF cookie and redirect
    response = _build_callback_redirect(
        code, state, redirect_uri, app, device_auth_service
    )
    response.delete_cookie(_CSRF_COOKIE_NAME)
    return response


@router.get("/auth/device/callback", include_in_schema=False)
@limiter.limit(Limits.DEVICE_AUTH)
async def device_callback(
    request: Request,
    code: str = "",
    state: str = "",
) -> Response:
    """Render the device auth callback page.

    The client reads the auth code and state from data attributes on the page.
    For browser extensions, the content script handles this automatically.
    """
    if not code:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(
        request, "device_callback.html", {"code": code, "state": state}
    )


@router.post(
    "/auth/device/token",
    responses=ERROR_RESPONSES,
    openapi_extra=PUBLIC_SECURITY,
    operation_id="exchangeDeviceCode",
    summary="Exchange Device Auth Code",
)
@limiter.limit(Limits.DEVICE_TOKEN)
async def device_token(
    request: Request,
    body: DeviceTokenRequest,
    device_auth_service: DeviceAuthSvc,
    grant_repo: AppGrantRepo,
) -> DeviceTokenResponse:
    """Exchange a one-time device auth code for JWT tokens.

    The code is obtained from the callback page after the user authenticates
    on spoo.me. Returns access and refresh tokens for the client.

    **Authentication**: Not required (public endpoint)

    **Rate Limits**: 10/min
    """
    result = await device_auth_service.exchange_device_code(body.code.strip())

    # Verify the grant is still active (closes the revoke race window)
    if result.app_id:
        grant = await grant_repo.find_active_grant(result.user.id, result.app_id)
        if not grant:
            raise AuthenticationError("app access has been revoked")
        try:
            await grant_repo.touch_last_used(result.user.id, result.app_id)
        except Exception:
            log.info(
                "touch_last_used_failed",
                user_id=str(result.user.id),
                app_id=result.app_id,
            )

    return DeviceTokenResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        user=UserProfileResponse.from_user(result.user),
    )


# ── App token refresh ─────────────────────────────────────────────────────────


@router.post(
    "/auth/device/refresh",
    responses=ERROR_RESPONSES,
    openapi_extra=PUBLIC_SECURITY,
    operation_id="refreshDeviceTokens",
    summary="Refresh Device Auth Tokens",
)
@limiter.limit(Limits.TOKEN_REFRESH)
async def device_refresh(
    request: Request,
    body: DeviceRefreshRequest,
    credential_service: CredentialSvc,
    grant_repo: AppGrantRepo,
) -> DeviceRefreshResponse:
    """Refresh an app's JWT tokens using a refresh token.

    Accepts the refresh token in the request body (not cookies) for use
    by external apps (browser extensions, desktop, CLI, bots).  If the
    refresh token contains an ``app_id`` claim, the server verifies the
    app grant is still active — revoked apps cannot refresh.

    **Authentication**: Not required (the refresh token itself is the credential)

    **Rate Limits**: 20/min
    """
    result = await credential_service.refresh_token(body.refresh_token)

    if result.app_id:
        grant = await grant_repo.find_active_grant(result.user.id, result.app_id)
        if not grant:
            raise AuthenticationError("app access has been revoked")
        try:
            await grant_repo.touch_last_used(result.user.id, result.app_id)
        except Exception:
            log.info(
                "touch_last_used_failed",
                user_id=str(result.user.id),
                app_id=result.app_id,
            )

    return DeviceRefreshResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
    )


# ── App revocation (dashboard action) ────────────────────────────────────────


@router.post("/auth/device/revoke", include_in_schema=False)
@limiter.limit(Limits.DEVICE_AUTH)
async def revoke_app(
    request: Request,
    user: JwtUser,
    device_auth_service: DeviceAuthSvc,
    grant_repo: AppGrantRepo,
    app_id: str = Form(""),
) -> Response:
    """Revoke an app's access (soft-delete grant + invalidate tokens).

    Protected against CSRF by requiring the X-Requested-With header,
    which cannot be sent by cross-origin form submissions.
    """
    if request.headers.get(_CSRF_HEADER_NAME) != _CSRF_HEADER_VALUE:
        return JSONResponse({"error": "invalid request"}, status_code=403)

    app_id = app_id.strip()
    if not app_id or len(app_id) > APP_ID_MAX_LEN:
        return JSONResponse({"error": "app_id is required"}, status_code=400)

    revoked = await grant_repo.revoke(user.user_id, app_id)
    if not revoked:
        return JSONResponse({"error": "no active grant found"}, status_code=404)

    # Invalidate device auth tokens bound to this app via the public service method
    await device_auth_service.revoke_device_tokens(user.user_id, app_id=app_id)

    return JSONResponse({"success": True, "message": f"Access revoked for {app_id}"})
