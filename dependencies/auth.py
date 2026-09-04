"""
Auth and identity dependency providers.

Resolves the current user from JWT or API key, and provides guards
(require_auth, require_verified_email) used by protected routes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Annotated

import jwt as pyjwt
import structlog
from bson import ObjectId
from fastapi import Depends, Request

from dependencies.infra import get_db, get_settings
from errors import (
    AccountPendingDeletionError,
    AuthenticationError,
    EmailNotVerifiedError,
    ForbiddenError,
)
from infrastructure.crypto import hash_token
from infrastructure.logging import get_logger
from repositories.api_key_repository import ApiKeyRepository
from repositories.user_repository import UserRepository
from schemas.dto.requests.api_key import ApiKeyScope
from schemas.models.api_key import ApiKeyDoc
from schemas.models.user import UserStatus
from shared.datetime_utils import as_aware_utc

log = get_logger(__name__)

# last_used_at is stamped at most once per key per debounce window, keeping
# the hot auth path effectively read-only.
_LAST_USED_DEBOUNCE = timedelta(hours=1)


@dataclass
class CurrentUser:
    """Resolved identity after auth check.

    ``api_key_doc`` is set when the request was authenticated via API key
    (``Authorization: Bearer spoo_<raw>``).  It is ``None`` for JWT auth.
    Scope checks inspect ``api_key_doc.scopes`` when present, otherwise
    ``scopes`` (the JWT ``scp`` claim) when not None.
    """

    user_id: ObjectId
    email_verified: bool
    api_key_doc: ApiKeyDoc | None = field(default=None)
    amr: str = "pwd"
    # Scope slugs from the JWT "scp" claim (device-auth app tokens).
    # None = unrestricted interactive session; [] would mean "no scopes".
    scopes: list[str] | None = field(default=None)
    # Connected-app id from the JWT "app_id" claim (device auth flow).
    app_id: str | None = field(default=None)
    # Lowercased user email — consumed by FeatureFlagService's ALLOWLIST
    # rollout (allowlist_emails). Populated from the "email" claim on the
    # JWT path and from the owning UserDoc on the API-key path. None for
    # access tokens minted before the claim existed; those users match by
    # user_id only until their next token refresh.
    email: str | None = field(default=None)
    # The JWT "plan" claim: a hint for the fail mode only, never authority.
    # None on the API-key path; the resolver reads the owner's plan by id.
    plan_claim: str | None = field(default=None)


async def get_current_user(
    request: Request,
    db=Depends(get_db),
) -> CurrentUser | None:
    """Resolve the current user from the Authorization header or access_token cookie.

    Auth resolution order (mirrors the existing Flask implementation):
      1. Authorization: Bearer spoo_<raw>  →  API key path
      2. Authorization: Bearer <jwt>        →  JWT path
      3. access_token cookie               →  JWT path
      4. None                              →  anonymous

    Returns None for anonymous requests. Never raises for malformed or
    invalid credentials — the one exception is a VALID API key whose
    account is pending deletion, which raises
    ``AccountPendingDeletionError`` (403 + ``X-Error-Code``) so keys go
    dark for the grace window instead of degrading to anonymous.
    """
    settings = get_settings(request)
    jwt_cfg = settings.jwt

    auth_header = request.headers.get("Authorization", "")
    token: str | None = None

    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()

        # ── API key path ──────────────────────────────────────────────────────
        if token.startswith("spoo_"):
            raw = token[len("spoo_") :]
            token_hash = hash_token(raw)
            key_repo = ApiKeyRepository(db["api-keys"])
            try:
                key = await key_repo.find_by_hash(token_hash)
            except Exception:
                return None

            # Failure logs carry the display prefix only — never hashes or
            # raw key material.
            if key is None:
                log.warning("api_key_auth_failed", reason="unknown", key_prefix=raw[:8])
                return None
            if key.revoked:
                log.warning(
                    "api_key_auth_failed",
                    reason="revoked",
                    key_prefix=key.token_prefix,
                    key_id=str(key.id),
                    user_id=str(key.user_id),
                )
                return None

            if key.paused_by_limit:
                log.warning(
                    "api_key_auth_failed",
                    reason="paused_by_limit",
                    key_prefix=key.token_prefix,
                    key_id=str(key.id),
                    user_id=str(key.user_id),
                )
                return None

            now = datetime.now(timezone.utc)
            exp = as_aware_utc(key.expires_at)
            if exp is not None and exp <= now:
                log.warning(
                    "api_key_auth_failed",
                    reason="expired",
                    key_prefix=key.token_prefix,
                    key_id=str(key.id),
                    user_id=str(key.user_id),
                )
                return None

            try:
                user = await UserRepository(db["users"]).find_by_id(key.user_id)
            except Exception:
                return None

            # Keys go dark during the deletion grace window — same error
            # surface as the login gate (INACTIVE stays ungated: login parity).
            if user is not None and user.status in (
                UserStatus.PENDING_DELETION,
                UserStatus.ERASING,
            ):
                log.warning(
                    "api_key_auth_blocked",
                    reason="pending_deletion",
                    key_prefix=key.token_prefix,
                    key_id=str(key.id),
                    user_id=str(key.user_id),
                )
                raise AccountPendingDeletionError(
                    "this account is scheduled for deletion"
                )

            # Best-effort last-used stamp — debounced so a busy key costs at
            # most one extra write per hour, and never fails the request.
            last_used = as_aware_utc(key.last_used_at)
            if last_used is None or now - last_used > _LAST_USED_DEBOUNCE:
                try:
                    await key_repo.touch_last_used(key.id)
                except Exception:
                    log.warning("api_key_touch_last_used_failed", key_id=str(key.id))

            email_verified = user.email_verified if user else False
            structlog.contextvars.bind_contextvars(
                user_id=str(key.user_id),
                auth_method="api_key",
                key_id=str(key.id),
                key_prefix=key.token_prefix,
            )
            # Contextvars bound here don't reach the middleware's own
            # request_completed log call (BaseHTTPMiddleware runs the
            # handler in a child task), so the identity is also stashed
            # on request.state, which is shared via the ASGI scope.
            request.state.auth_ctx = {
                "user_id": str(key.user_id),
                "auth_method": "api_key",
                "key_id": str(key.id),
                "key_prefix": key.token_prefix,
            }
            return CurrentUser(
                user_id=key.user_id,
                email_verified=email_verified,
                api_key_doc=key,
                # The owning UserDoc is already fetched above for
                # email_verified — no extra DB hit to carry the email.
                email=user.email.lower() if user and user.email else None,
            )

    # ── JWT path ──────────────────────────────────────────────────────────────
    if not token:
        token = request.cookies.get("access_token")

    if not token:
        return None

    try:
        algorithm = "RS256" if jwt_cfg.use_rs256 else "HS256"
        verify_key = jwt_cfg.jwt_public_key if jwt_cfg.use_rs256 else jwt_cfg.jwt_secret
        claims = pyjwt.decode(
            token,
            verify_key,
            algorithms=[algorithm],
            issuer=jwt_cfg.jwt_issuer,
            audience=jwt_cfg.jwt_audience,
        )
        # Reject refresh tokens used as access tokens
        if claims.get("type") == "refresh":
            return None
        user_id = ObjectId(claims["sub"])
        email_verified = bool(claims.get("email_verified", False))
        amr = claims.get("amr", ["pwd"])[0]
        # Tolerant read — access tokens minted before the "email" claim
        # existed simply carry email=None (never an error). Fixed on the
        # user's next token refresh.
        raw_email = claims.get("email")
        email = (
            raw_email.strip().lower()
            if isinstance(raw_email, str) and raw_email.strip()
            else None
        )
        # Device-auth app tokens carry scp + app_id; session tokens carry
        # neither. A malformed scp claim fails closed (empty scope list)
        # rather than falling back to unrestricted.
        raw_scopes = claims.get("scp")
        scopes: list[str] | None = None
        if raw_scopes is not None:
            scopes = (
                [s for s in raw_scopes if isinstance(s, str)]
                if isinstance(raw_scopes, list)
                else []
            )
        structlog.contextvars.bind_contextvars(user_id=str(user_id), auth_method="jwt")
        # Same request.state stash as the API-key path — see comment there.
        auth_ctx = {"user_id": str(user_id), "auth_method": "jwt"}
        if claims.get("app_id") is not None:
            auth_ctx["app_id"] = claims["app_id"]
        request.state.auth_ctx = auth_ctx
        return CurrentUser(
            user_id=user_id,
            email_verified=email_verified,
            amr=amr,
            email=email,
            plan_claim=claims.get("plan"),
            scopes=scopes,
            app_id=claims.get("app_id"),
        )
    except Exception:
        return None


async def require_auth(
    user: CurrentUser | None = Depends(get_current_user),
) -> CurrentUser:
    """Raise 401 if the request is not authenticated."""
    if user is None:
        raise AuthenticationError("Authentication required")
    return user


async def require_verified_email(
    user: CurrentUser = Depends(require_auth),
) -> CurrentUser:
    """Raise 403 (EMAIL_NOT_VERIFIED) if the user's email is unverified."""
    if not user.email_verified:
        raise EmailNotVerifiedError("Email verification required")
    return user


async def require_jwt(
    user: CurrentUser = Depends(require_auth),
) -> CurrentUser:
    """Raise 403 unless the request comes from an interactive session.

    Rejects API keys AND scoped device-app tokens (``scp`` claim). Use on
    account-security surfaces — profile, app management, device revoke —
    where a delegated credential must never act.
    """
    if user.api_key_doc is not None:
        raise ForbiddenError("API keys cannot be used to manage API keys")
    # Delegation is marked by app_id, not scp: a legacy grant mints an
    # app token with app_id but no scp, and it must be barred here too.
    if user.app_id is not None:
        raise ForbiddenError("This operation requires an interactive session")
    return user


async def require_jwt_verified(
    user: CurrentUser = Depends(require_jwt),
) -> CurrentUser:
    """JWT-only auth + verified email check."""
    if not user.email_verified:
        raise EmailNotVerifiedError("Email verification required")
    return user


def _granted_scopes(user: CurrentUser) -> set[str] | None:
    """The scope set a credential holds, or None for unrestricted sessions."""
    if user.api_key_doc is not None:
        return set(user.api_key_doc.scopes)
    if user.scopes is not None:
        return set(user.scopes)
    return None


def check_credential_scopes(
    user: CurrentUser | None, required_scopes: set[str]
) -> None:
    """Raise ForbiddenError if a scoped credential lacks a required scope.

    Fires for API keys (key scopes) and device-app tokens (``scp`` claim);
    interactive sessions and anonymous requests are not scope-restricted.
    A single match against ``required_scopes`` suffices (OR semantics).
    """
    if user is None:
        return
    granted = _granted_scopes(user)
    if granted is not None and not granted & required_scopes:
        raise ForbiddenError("Insufficient scope for this operation")


# Back-compat alias — the check now covers app tokens too.
check_api_key_scope = check_credential_scopes


# ── Named scope sets ─────────────────────────────────────────────────────────

STATS_SCOPES: set[str] = {
    ApiKeyScope.STATS_READ,
    ApiKeyScope.URLS_READ,
    ApiKeyScope.ADMIN_ALL,
}
URL_MANAGEMENT_SCOPES: set[str] = {ApiKeyScope.URLS_MANAGE, ApiKeyScope.ADMIN_ALL}
URL_READ_SCOPES: set[str] = {
    ApiKeyScope.URLS_MANAGE,
    ApiKeyScope.URLS_READ,
    ApiKeyScope.ADMIN_ALL,
}
SHORTEN_SCOPES: set[str] = {ApiKeyScope.SHORTEN_CREATE, ApiKeyScope.ADMIN_ALL}
REPORTS_SCOPES: set[str] = {ApiKeyScope.REPORTS_CREATE, ApiKeyScope.ADMIN_ALL}
DOMAIN_MANAGE_SCOPES: set[str] = {ApiKeyScope.DOMAINS_MANAGE, ApiKeyScope.ADMIN_ALL}
DOMAIN_READ_SCOPES: set[str] = {
    ApiKeyScope.DOMAINS_MANAGE,
    ApiKeyScope.DOMAINS_READ,
    ApiKeyScope.ADMIN_ALL,
}
WEBHOOKS_MANAGE_SCOPES: set[str] = {
    ApiKeyScope.WEBHOOKS_MANAGE,
    ApiKeyScope.ADMIN_ALL,
}
WEBHOOKS_READ_SCOPES: set[str] = {
    ApiKeyScope.WEBHOOKS_MANAGE,
    ApiKeyScope.WEBHOOKS_READ,
    ApiKeyScope.ADMIN_ALL,
}
# Deliberately NOT satisfied by admin:all: API keys can hold admin:all but
# must never manage keys, and app tokens declare keys:manage explicitly.
KEYS_MANAGE_SCOPES: set[str] = {ApiKeyScope.KEYS_MANAGE}


# ── Parameterised scope dependency factories ─────────────────────────────────


def require_scopes(scopes: set[str]):
    """Dependency factory: require_auth + API-key scope check.

    Moves scope enforcement into the dependency graph so route handlers
    receive an already-validated ``CurrentUser``.

    Usage::

        user: CurrentUser = Depends(require_scopes(URL_MANAGEMENT_SCOPES))
    """

    async def _dep(user: CurrentUser = Depends(require_auth)) -> CurrentUser:
        check_credential_scopes(user, scopes)
        return user

    return _dep


def require_session_or_scopes(scopes: set[str]):
    """Dependency factory: interactive session OR a credential holding *scopes*.

    Passes for an interactive session (no API key, no ``app_id``), or for a
    delegated credential whose scope set intersects *scopes*. A delegated
    credential is identified by ``api_key_doc``/``app_id``, not by ``scp``,
    so a legacy app token (``app_id`` but no ``scp``) is treated as delegated
    and denied — it predates ``keys:manage`` and never held it. API keys can
    never be created with ``keys:manage`` (see ALLOWED_SCOPES), so the
    anti-self-propagation guard holds.
    """

    async def _dep(user: CurrentUser = Depends(require_auth)) -> CurrentUser:
        if user.api_key_doc is None and user.app_id is None:
            return user  # interactive session — unrestricted
        granted = _granted_scopes(user)
        if not granted or not granted & scopes:
            raise ForbiddenError("Insufficient scope for this operation")
        return user

    return _dep


def optional_scopes(scopes: set[str]):
    """Dependency factory: get_current_user + optional API-key scope check.

    Does not require authentication; raises only when an API key is used
    and it lacks the required scope.

    Usage::

        user: Optional[CurrentUser] = Depends(optional_scopes(SHORTEN_SCOPES))
    """

    async def _dep(
        user: CurrentUser | None = Depends(get_current_user),
    ) -> CurrentUser | None:
        check_credential_scopes(user, scopes)
        return user

    return _dep


# ── Composed dependencies ────────────────────────────────────────────────────


def optional_scopes_verified(scopes: set[str]):
    """Like ``optional_scopes`` but requires email verification when authenticated.

    Anonymous requests pass through. Authenticated users without a verified
    email are rejected with ``EmailNotVerifiedError``.

    Use for optional-auth endpoints that create resources.
    """

    async def _dep(
        user: CurrentUser | None = Depends(optional_scopes(scopes)),
    ) -> CurrentUser | None:
        if user is not None and not user.email_verified:
            raise EmailNotVerifiedError("Email verification required")
        return user

    return _dep


def require_scopes_verified(scopes: set[str]):
    """``require_scopes`` plus email-verification gate.

    Use on protected create endpoints that accept BOTH JWT and API key auth.
    API key callers must also be email-verified (their underlying user must
    have verified the email at signup time).
    """

    async def _dep(
        user: CurrentUser = Depends(require_scopes(scopes)),
    ) -> CurrentUser:
        if not user.email_verified:
            raise EmailNotVerifiedError("Email verification required")
        return user

    return _dep


# ── Named dependency instances ────────────────────────────────────────────────

# Key listing/deletion: interactive session OR a credential holding
# keys:manage. Key *creation* is not here — minting a new credential is a
# first-party act, so create uses JwtVerifiedUser (interactive session only).
require_keys_access = require_session_or_scopes(KEYS_MANAGE_SCOPES)


# ── Annotated type aliases — community-standard Depends shortcuts ─────────────

AuthUser = Annotated[CurrentUser, Depends(require_auth)]
VerifiedUser = Annotated[CurrentUser, Depends(require_verified_email)]
OptionalUser = Annotated[CurrentUser | None, Depends(get_current_user)]
JwtUser = Annotated[CurrentUser, Depends(require_jwt)]
JwtVerifiedUser = Annotated[CurrentUser, Depends(require_jwt_verified)]
KeysAccessUser = Annotated[CurrentUser, Depends(require_keys_access)]
