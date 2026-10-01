"""
Core auth routes — login, register, refresh, logout, profile, set-password.

GET  /login            → redirect /
GET  /register         → redirect /
GET  /signup           → redirect /
POST /auth/login
POST /auth/register
POST /auth/refresh
POST /auth/logout
POST /auth/restore
GET  /auth/me
PATCH /auth/me
POST /auth/set-password
"""

from __future__ import annotations

from bson import ObjectId
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from dependencies import (
    AccountDeletionSvc,
    AuthUser,
    CredentialSvc,
    Entitled,
    EntitlementSvc,
    JwtConfig,
    JwtUser,
    PasswordSvc,
    ProfilePictureSvc,
    UserRepo,
    fetch_user_profile,
)
from errors import AuthenticationError
from infrastructure.logging import get_logger
from middleware.openapi import AUTH_RESPONSES, ERROR_RESPONSES, PUBLIC_SECURITY
from middleware.rate_limiter import Limits, limiter
from routes.cookie_helpers import clear_auth_cookies, set_auth_cookies
from schemas.dto.requests.account import RestoreAccountRequest
from schemas.dto.requests.auth import (
    LoginRequest,
    RegisterRequest,
    SetPasswordRequest,
    UpdateProfileRequest,
)
from schemas.dto.responses.auth import (
    LoginResponse,
    LogoutResponse,
    MeResponse,
    RefreshResponse,
    RegisterResponse,
    UserProfileResponse,
)
from schemas.dto.responses.common import ErrorResponse, MessageResponse
from shared.ip_utils import get_client_ip

log = get_logger(__name__)

router = APIRouter()


# ── Redirect shortcuts ────────────────────────────────────────────────────────


@router.get("/login", include_in_schema=False)
async def login_redirect() -> RedirectResponse:
    """Redirect /login → / to prevent it being captured as a short-URL alias."""
    return RedirectResponse("/", status_code=302)


@router.get("/register", include_in_schema=False)
async def register_redirect() -> RedirectResponse:
    """Redirect /register → / to prevent it being captured as a short-URL alias."""
    return RedirectResponse("/", status_code=302)


@router.get("/signup", include_in_schema=False)
async def signup_redirect() -> RedirectResponse:
    """Redirect /signup → / to prevent it being captured as a short-URL alias."""
    return RedirectResponse("/", status_code=302)


# ── Auth endpoints ─────────────────────────────────────────────────────────────


@router.post(
    "/auth/login",
    responses=ERROR_RESPONSES,
    openapi_extra=PUBLIC_SECURITY,
    operation_id="loginUser",
    summary="Login",
)
@limiter.limit(Limits.LOGIN)
async def login(
    request: Request,
    response: Response,
    body: LoginRequest,
    credential_service: CredentialSvc,
    entitlement_service: EntitlementSvc,
    jwt_cfg: JwtConfig,
) -> LoginResponse:
    """Authenticate with email and password.

    Returns JWT access token and sets secure HTTP-only cookies for both
    access and refresh tokens. The refresh token can be used at
    ``POST /auth/refresh`` to obtain new tokens without re-authenticating.

    **Authentication**: Not required (public endpoint)

    **Rate Limits**: 5/min, 50/day

    **Security**: Returns identical error for wrong email and wrong password
    to prevent user enumeration.
    """
    email = body.email.strip().lower()
    result = await credential_service.login(email, body.password)
    set_auth_cookies(response, result.access_token, result.refresh_token, jwt_cfg)
    return LoginResponse(
        access_token=result.access_token,
        user=UserProfileResponse.from_user(
            result.user, plan=await entitlement_service.plan_hint_for(result.user.id)
        ),
    )


@router.post(
    "/auth/register",
    status_code=201,
    responses=AUTH_RESPONSES,
    openapi_extra=PUBLIC_SECURITY,
    operation_id="registerUser",
    summary="Register",
)
@limiter.limit(Limits.SIGNUP)
async def register(
    request: Request,
    response: Response,
    body: RegisterRequest,
    credential_service: CredentialSvc,
    entitlement_service: EntitlementSvc,
    jwt_cfg: JwtConfig,
) -> RegisterResponse:
    """Create a new user account with email and password.

    Immediately signs the user in by returning a JWT access token and setting
    secure HTTP-only cookies. A verification email is sent best-effort; the
    ``verification_sent`` field indicates whether it succeeded.

    **Authentication**: Not required (public endpoint)

    **Rate Limits**: 5/min, 50/day

    **Notes**: The account is created even if the verification email fails.
    The user must verify their email before accessing protected resources.
    """
    email = body.email.strip().lower()
    user_name = (body.user_name or "").strip() or None
    client_ip = get_client_ip(request)
    result = await credential_service.register(
        email, body.password, user_name, client_ip
    )
    set_auth_cookies(response, result.access_token, result.refresh_token, jwt_cfg)
    return RegisterResponse(
        access_token=result.access_token,
        user=UserProfileResponse.from_user(
            result.user, plan=await entitlement_service.plan_hint_for(result.user.id)
        ),
        requires_verification=True,
        verification_sent=result.verification_sent,
    )


@router.post(
    "/auth/refresh",
    response_model=RefreshResponse,
    responses=ERROR_RESPONSES,
    openapi_extra=PUBLIC_SECURITY,
    operation_id="refreshTokens",
    summary="Refresh Tokens",
)
@limiter.limit(Limits.TOKEN_REFRESH)
async def refresh(
    request: Request,
    credential_service: CredentialSvc,
    jwt_cfg: JwtConfig,
) -> Response:
    """Rotate the access and refresh token pair.

    Reads the ``refresh_token`` cookie, validates it, and issues a new
    access/refresh pair. Both cookies are replaced. If the refresh token
    is missing, expired, or invalid, all auth cookies are cleared and a
    401 response is returned.

    **Authentication**: Requires a valid ``refresh_token`` cookie

    **Rate Limits**: 20/min

    **Notes**: The old refresh token is invalidated after use (rotation).
    """
    refresh_token_str = request.cookies.get("refresh_token")
    if not refresh_token_str:
        resp = JSONResponse(
            AuthenticationError("missing refresh token").to_dict(),
            status_code=401,
        )
        clear_auth_cookies(resp, jwt_cfg)
        return resp

    try:
        result = await credential_service.refresh_token(refresh_token_str)
    except AuthenticationError as exc:
        log.info("token_refresh_failed", error=str(exc))
        resp = JSONResponse(
            AuthenticationError("invalid or expired refresh token").to_dict(),
            status_code=401,
        )
        clear_auth_cookies(resp, jwt_cfg)
        return resp

    resp = JSONResponse(RefreshResponse(access_token=result.access_token).model_dump())
    set_auth_cookies(resp, result.access_token, result.refresh_token, jwt_cfg)
    return resp


@router.post(
    "/auth/logout",
    operation_id="logout",
    summary="Logout",
)
@limiter.limit(Limits.LOGOUT)
async def logout(
    request: Request,
    response: Response,
    jwt_cfg: JwtConfig,
) -> LogoutResponse:
    """Log the current user out by clearing auth cookies.

    Removes the ``access_token`` and ``refresh_token`` HTTP-only cookies.
    Always succeeds regardless of whether the user was authenticated.

    **Authentication**: Not required

    **Rate Limits**: 60/hour
    """
    clear_auth_cookies(response, jwt_cfg)
    return LogoutResponse(success=True)


@router.post(
    "/auth/restore",
    responses={
        **ERROR_RESPONSES,
        # The uniform anti-enumeration failure (see the docstring) — the
        # generic "insufficient permissions" text would misdocument it.
        403: {
            "description": (
                "Forbidden — invalid credentials, unknown email or token, "
                "or account is not pending deletion"
            ),
            "model": ErrorResponse,
        },
    },
    openapi_extra=PUBLIC_SECURITY,
    operation_id="restoreAccount",
    summary="Restore Account",
)
# Tightest account-security budget (3/hour) — an unauthenticated
# credential check, same guessing surface as a password reset.
@limiter.limit(Limits.PASSWORD_RESET_REQUEST)
async def restore_account(
    request: Request,
    body: RestoreAccountRequest,
    deletion_service: AccountDeletionSvc,
) -> MessageResponse:
    """Cancel a pending account deletion during the grace period.

    Two mutually exclusive proofs: ``email`` + ``password`` validates the
    account credentials, ``restore_token`` consumes the one-shot link from
    the deletion notice email (the only path for OAuth-only accounts).
    Either flips the account back to ACTIVE, clears the purge deadline,
    and sends a cancellation notice to the account address. Only works
    while the account is PENDING_DELETION. The hard boundary is the
    erasure sweep claiming the account (typically within minutes of the
    ``purge_after`` deadline from ``DELETE /api/v1/me``), so a restore in
    the window between the deadline and the claim still succeeds; once
    erasure has started, restore is refused for good.

    **Authentication**: Not required (public endpoint)

    **Rate Limits**: 3/hour

    **Security**: Returns a uniform 403 for wrong credentials, unknown
    email, invalid or spent tokens, and accounts not pending deletion —
    prevents account enumeration and state probing.
    """
    if body.restore_token is not None:
        await deletion_service.restore_with_token(body.restore_token)
    else:
        # The DTO guarantees both are present on this branch.
        await deletion_service.restore(str(body.email), str(body.password))
    return MessageResponse(success=True, message="account restored")


@router.get(
    "/auth/me",
    responses=ERROR_RESPONSES,
    operation_id="getCurrentUser",
    summary="Get Current User",
)
@limiter.limit(Limits.AUTH_READ)
async def me(
    request: Request,
    user: AuthUser,
    user_repo: UserRepo,
    entitlements: Entitled,
) -> MeResponse:
    """Return the authenticated user's full profile.

    Includes email, verification status, linked OAuth providers, plan,
    and profile picture. Useful for populating the UI after login or
    on page load.

    **Authentication**: Required (JWT or API key)

    **Rate Limits**: 60/min
    """
    profile = await fetch_user_profile(user_repo, ObjectId(str(user.user_id)))
    return MeResponse(
        user=UserProfileResponse.from_user(profile, plan=entitlements.plan.value)
    )


@router.patch(
    "/auth/me",
    responses=ERROR_RESPONSES,
    operation_id="updateCurrentUser",
    summary="Update Current User",
)
@limiter.limit(Limits.PROFILE_UPDATE)
async def update_me(
    request: Request,
    body: UpdateProfileRequest,
    user: JwtUser,
    profile_service: ProfilePictureSvc,
    entitlements: Entitled,
) -> MeResponse:
    """Update the authenticated user's profile.

    Currently only ``user_name`` is editable. Registration makes the
    display name write-once, so accounts that skipped it need this to
    set one later. Send a string to set the name or ``null`` to clear it.

    **Authentication**: Required (JWT only — API keys cannot rename the account)

    **Rate Limits**: 10/min
    """
    # Same normalization as register: strip, whitespace-only → not set.
    user_name = (body.user_name or "").strip() or None
    updated = await profile_service.update_user_name(
        ObjectId(str(user.user_id)), user_name
    )
    return MeResponse(
        user=UserProfileResponse.from_user(updated, plan=entitlements.plan.value)
    )


@router.post(
    "/auth/set-password",
    responses=ERROR_RESPONSES,
    operation_id="setPassword",
    summary="Set Password",
)
@limiter.limit(Limits.SET_PASSWORD)
async def set_password(
    request: Request,
    body: SetPasswordRequest,
    user: JwtUser,
    password_service: PasswordSvc,
) -> MessageResponse:
    """Set a password for an OAuth-only account.

    Allows users who signed up via OAuth to add a password so they can
    also log in with email + password. Fails if the user already has a
    password set.

    **Authentication**: Required (JWT only — API keys cannot set passwords)

    **Rate Limits**: 5/min
    """
    await password_service.set_password(str(user.user_id), body.password)
    return MessageResponse(success=True, message="password set successfully")
