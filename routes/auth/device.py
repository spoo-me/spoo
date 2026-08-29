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
    DeviceAuthSvc,
    JwtConfig,
    JwtUser,
    OptionalUser,
    UserRepo,
    fetch_user_profile,
)
from errors import (
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
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
from services.auth.device import APP_ID_MAX_LEN, is_valid_pkce_challenge
from shared.generators import generate_secure_token
from shared.scopes import describe_scopes

log = get_logger(__name__)

router = APIRouter()

# ── Constants (CSRF is a route-layer concern) ────────────────────────────────

_CSRF_COOKIE_NAME = "_consent_csrf"
_CSRF_TTL_SECONDS = 600
_CSRF_TOKEN_BYTES = 32
_CSRF_HEADER_NAME = "x-requested-with"
_CSRF_HEADER_VALUE = "fetch"

_PKCE_ERROR_MESSAGE = (
    "This app's sign-in request is missing a valid security challenge "
    "(PKCE). Please update the app to its latest version and try again."
)


# ── Response builders ────────────────────────────────────────────────────────


def _device_error(request: Request, error: str, status_code: int = 400) -> Response:
    """Render the device auth error page."""
    return templates.TemplateResponse(
        request, "device_error.html", {"error": error}, status_code=status_code
    )


def _build_callback_redirect(
    code: str, state: str, redirect_uri: str, app: AppEntry
) -> RedirectResponse:
    """Build the redirect to the callback page or a registered redirect_uri."""
    params = urlencode({"code": code, "state": state})
    if redirect_uri and redirect_uri in app.redirect_uris:
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
    code_challenge: str = "",
    code_challenge_method: str = "",
) -> Response:
    """Initiate the device auth flow with app identification and consent.

    Validates the app_id against the registry and the PKCE challenge
    (mandatory, S256 only). If the user has an existing active grant,
    auto-approves and generates a code. Otherwise shows the consent screen.
    """
    app = device_auth_service.resolve_app(app_id)
    if not app:
        return _device_error(request, "Unknown or unsupported application")

    if not device_auth_service.validate_redirect_uri(redirect_uri, app):
        return _device_error(request, "Invalid redirect URI for this application")

    if not is_valid_pkce_challenge(code_challenge, code_challenge_method):
        return _device_error(request, _PKCE_ERROR_MESSAGE)

    if not user:
        params: dict[str, str] = {
            "app_id": app_id,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
        }
        if state:
            params["state"] = state
        if redirect_uri:
            params["redirect_uri"] = redirect_uri
        next_url = f"/auth/device/login?{urlencode(params)}"
        # /login is the only frontend surface that resumes ?next= after auth.
        return RedirectResponse(f"/login?next={quote(next_url)}", status_code=302)

    # Check for existing active grant (auto-approve)
    grant = await grant_repo.find_active_grant(user.user_id, app_id)
    if grant:
        profile = await fetch_user_profile(user_repo, ObjectId(str(user.user_id)))
        code = await device_auth_service.create_device_auth_code(
            profile.id, profile.email, code_challenge=code_challenge, app_id=app_id
        )
        return _build_callback_redirect(code, state, redirect_uri, app)

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
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "csrf_token": csrf_token,
            "user": profile,
            "permissions": (
                describe_scopes(app.scopes) if app.scopes else list(app.permissions)
            ),
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
    code_challenge: str = Form(""),
    code_challenge_method: str = Form(""),
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

    # Re-validate the challenge — the hidden form field is attacker-writable,
    # so nothing that skipped the shape check may reach code minting.
    if not is_valid_pkce_challenge(code_challenge, code_challenge_method):
        return _device_error(request, _PKCE_ERROR_MESSAGE)

    # Create grant, snapshotting the registry scopes at approval time.
    # The loader guarantees live apps declare scopes; `or None` keeps a
    # programmatically-built scopeless entry on legacy (unrestricted)
    # semantics instead of minting a bricked empty-scope grant.
    granted_scopes = [scope.value for scope in app.scopes] or None
    await grant_repo.create_or_reactivate(user.user_id, app_id, scopes=granted_scopes)
    log.info(
        "app_consent_granted",
        user_id=str(user.user_id),
        app_id=app_id,
        scopes=granted_scopes,
    )

    # Generate device auth code
    profile = await fetch_user_profile(user_repo, ObjectId(str(user.user_id)))
    code = await device_auth_service.create_device_auth_code(
        profile.id, profile.email, code_challenge=code_challenge, app_id=app_id
    )

    # Clear CSRF cookie and redirect
    response = _build_callback_redirect(code, state, redirect_uri, app)
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
) -> DeviceTokenResponse:
    """Exchange a one-time device auth code for JWT tokens.

    The code is obtained from the callback page after the user authenticates
    on spoo.me. The PKCE ``code_verifier`` must match the ``code_challenge``
    the app sent when initiating the flow. Returns access and refresh tokens
    scoped to the app's grant.

    **Authentication**: Not required (public endpoint)

    **Rate Limits**: 10/min
    """
    result = await device_auth_service.exchange_device_code(
        body.code.strip(), body.code_verifier
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
    device_auth_service: DeviceAuthSvc,
) -> DeviceRefreshResponse:
    """Refresh an app's JWT tokens using a refresh token.

    Accepts the refresh token in the request body (not cookies) for use
    by external apps (browser extensions, desktop, CLI, bots).  If the
    refresh token contains an ``app_id`` claim, the server verifies the
    app grant is still active — revoked apps cannot refresh — and re-reads
    the grant's scopes so scope changes propagate on the next refresh.

    **Authentication**: Not required (the refresh token itself is the credential)

    **Rate Limits**: 20/min
    """
    result = await device_auth_service.refresh_device_tokens(body.refresh_token)

    return DeviceRefreshResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
    )


# ── App revocation (dashboard action) ────────────────────────────────────────


async def _extract_revoke_target(
    request: Request, user_id: ObjectId, grant_repo
) -> str:
    """Resolve the app_id to revoke from a form or JSON request body.

    JSON bodies may carry ``app_id`` and/or ``grant_id`` (the grant
    document id, resolved to its app_id for this user). Form bodies carry
    ``app_id`` only (legacy dashboard shape).
    """
    content_type = request.headers.get("content-type", "")
    app_id = ""
    grant_id = ""
    if "application/json" in content_type:
        try:
            body = await request.json()
        except Exception:
            raise ValidationError("invalid JSON body") from None
        if not isinstance(body, dict):
            raise ValidationError("invalid JSON body")
        app_id = str(body.get("app_id") or "").strip()
        grant_id = str(body.get("grant_id") or "").strip()
    else:
        form = await request.form()
        app_id = str(form.get("app_id") or "").strip()

    if app_id:
        if len(app_id) > APP_ID_MAX_LEN:
            raise ValidationError("app_id is required")
        return app_id

    if grant_id:
        try:
            grant_oid = ObjectId(grant_id)
        except Exception:
            raise ValidationError("invalid grant_id") from None
        grant = await grant_repo.find_by_id_for_user(user_id, grant_oid)
        if not grant or grant.revoked_at is not None:
            raise NotFoundError("no active grant found")
        return grant.app_id

    raise ValidationError("app_id or grant_id is required")


@router.post("/auth/device/revoke", include_in_schema=False)
@limiter.limit(Limits.DEVICE_AUTH)
async def revoke_app(
    request: Request,
    user: JwtUser,
    device_auth_service: DeviceAuthSvc,
    grant_repo: AppGrantRepo,
) -> Response:
    """Revoke an app's access (soft-delete grant + invalidate tokens).

    Accepts a form body with ``app_id`` (legacy dashboard) or a JSON body
    with ``app_id`` and/or ``grant_id``. Protected against CSRF by
    requiring the X-Requested-With header, which cannot be sent by
    cross-origin form submissions.
    """
    if request.headers.get(_CSRF_HEADER_NAME) != _CSRF_HEADER_VALUE:
        raise ForbiddenError("invalid request")

    app_id = await _extract_revoke_target(request, user.user_id, grant_repo)

    revoked = await grant_repo.revoke(user.user_id, app_id)
    if not revoked:
        raise NotFoundError("no active grant found")

    # Invalidate device auth tokens bound to this app via the public service method
    await device_auth_service.revoke_device_tokens(user.user_id, app_id=app_id)

    return JSONResponse({"success": True, "message": f"Access revoked for {app_id}"})
