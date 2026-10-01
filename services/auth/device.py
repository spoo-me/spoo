"""
DeviceAuthService — device/extension authentication flow.

Handles the OAuth-like device auth flow used by browser extensions, CLIs,
and desktop apps.  Creates one-time auth codes (PKCE-bound), exchanges
them for JWT tokens, refreshes app tokens, and revokes device tokens on
app unlink.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone

from bson import ObjectId

from errors import AuthenticationError
from infrastructure.crypto import hash_token, pkce_s256_challenge
from infrastructure.logging import get_logger
from repositories.app_grant_repository import AppGrantRepository
from repositories.token_repository import TokenRepository
from repositories.user_repository import UserRepository
from schemas.models.app import AppEntry
from schemas.models.app_grant import AppGrantDoc
from schemas.models.token import TOKEN_TYPE_DEVICE_AUTH
from schemas.models.user import UserStatus
from schemas.results import AuthResult
from services.token_factory import TokenFactory
from shared.generators import generate_secure_token

log = get_logger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

DEVICE_AUTH_EXPIRY_SECONDS = 300  # 5 minutes
APP_ID_MAX_LEN = 64

# S256 challenge: base64url(sha256) is always exactly 43 chars, unpadded.
PKCE_CHALLENGE_METHOD = "S256"
_PKCE_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")

_INVALID_CODE_MESSAGE = "invalid or expired device auth code"


def is_valid_pkce_challenge(code_challenge: str, code_challenge_method: str) -> bool:
    """Validate the PKCE parameters of a device auth request (RFC 7636).

    Only S256 is accepted — ``plain`` would store the verifier itself,
    defeating the point of hashing.
    """
    return code_challenge_method == PKCE_CHALLENGE_METHOD and bool(
        _PKCE_CHALLENGE_RE.fullmatch(code_challenge)
    )


def effective_scopes_for(
    grant: AppGrantDoc | None, entry: AppEntry | None
) -> list[str] | None:
    """Resolve the scopes a grant confers.

    Grant snapshot wins; registry entry is the fallback for grants that
    predate scoped consent. ``None`` means legacy-unrestricted (the grant
    predates scopes AND its app has no registry scopes to inherit).
    """
    if grant is not None and grant.scopes is not None:
        return list(grant.scopes)
    if entry is not None and entry.scopes:
        return [scope.value for scope in entry.scopes]
    return None


class DeviceAuthService:
    """Device/extension authentication flow.

    Args:
        user_repo:     Repository for the ``users`` collection.
        token_repo:    Repository for the ``verification-tokens`` collection.
        token_factory: JWT token generation.
        grant_repo:    Repository for the ``app-grants`` collection.
        app_registry:  App registry loaded from apps.yaml at startup.
    """

    def __init__(
        self,
        user_repo: UserRepository,
        token_repo: TokenRepository,
        token_factory: TokenFactory,
        grant_repo: AppGrantRepository,
        app_registry: dict[str, AppEntry] | None = None,
    ) -> None:
        self._user_repo = user_repo
        self._token_repo = token_repo
        self._tokens = token_factory
        self._grant_repo = grant_repo
        self._app_registry: dict[str, AppEntry] = app_registry or {}

    # ── Validation ───────────────────────────────────────────────────────────

    def resolve_app(self, app_id: str) -> AppEntry | None:
        """Look up an app_id in the registry.

        Returns the AppEntry if it's a live device-auth app, None otherwise.
        """
        if not app_id or len(app_id) > APP_ID_MAX_LEN:
            return None
        entry = self._app_registry.get(app_id)
        return entry if entry and entry.is_live_device_app() else None

    def validate_redirect_uri(self, redirect_uri: str, app: AppEntry) -> bool:
        """Return True if redirect_uri is empty or in the app's allowlist."""
        return not redirect_uri or redirect_uri in app.redirect_uris

    def resolve_effective_scopes(self, grant: AppGrantDoc) -> list[str] | None:
        """Effective scopes for *grant*: snapshot, else registry, else None."""
        return effective_scopes_for(grant, self._app_registry.get(grant.app_id))

    # ── Code lifecycle ────────────────────────────────────────────────────────

    async def create_device_auth_code(
        self,
        user_id: ObjectId,
        email: str,
        *,
        code_challenge: str,
        app_id: str | None = None,
    ) -> str:
        """Generate a one-time auth code for the device auth flow.

        ``code_challenge`` is the client's S256 PKCE challenge — already a
        hash, stored verbatim and checked at exchange time.

        Returns the raw token (caller redirects to callback with it).
        """
        svc_log = log.bind(op="auth.device_create_code")

        await self._token_repo.delete_by_user(
            user_id, TOKEN_TYPE_DEVICE_AUTH, app_id=app_id
        )

        raw_token = generate_secure_token(48)
        now = datetime.now(timezone.utc)
        token_data: dict = {
            "user_id": user_id,
            "email": email,
            "token_hash": hash_token(raw_token),
            "token_type": TOKEN_TYPE_DEVICE_AUTH,
            "expires_at": now + timedelta(seconds=DEVICE_AUTH_EXPIRY_SECONDS),
            "created_at": now,
            "used_at": None,
            "attempts": 0,
            "code_challenge": code_challenge,
        }
        if app_id:
            token_data["app_id"] = app_id
        await self._token_repo.create(token_data)
        svc_log.info("device_auth_code_created", user_id=str(user_id), app_id=app_id)
        return raw_token

    async def exchange_device_code(self, code: str, code_verifier: str) -> AuthResult:
        """Exchange a one-time device auth code + PKCE verifier for JWT tokens.

        Verifies S256(code_verifier) against the challenge bound to the code,
        re-checks the app grant (closing the revoke race window), and mints
        tokens carrying the grant's effective scopes.

        Raises:
            AuthenticationError: Code invalid, expired, already used, PKCE
                verifier mismatch (deliberately indistinguishable), or the
                grant was revoked.
        """
        svc_log = log.bind(op="auth.device_exchange")

        token_hash = hash_token(code)
        token_doc = await self._token_repo.consume_by_hash(
            token_hash, TOKEN_TYPE_DEVICE_AUTH
        )
        if not token_doc:
            raise AuthenticationError(_INVALID_CODE_MESSAGE)

        # PKCE (mandatory): codes are minted with a challenge; a stored code
        # without one is rejected outright. Failures use the same generic
        # message as a bad code — no oracle for which check failed.
        if not token_doc.code_challenge or not secrets.compare_digest(
            pkce_s256_challenge(code_verifier), token_doc.code_challenge
        ):
            svc_log.info(
                "device_auth_pkce_failed",
                user_id=str(token_doc.user_id),
                app_id=token_doc.app_id,
            )
            raise AuthenticationError(_INVALID_CODE_MESSAGE)

        user = await self._user_repo.find_by_id(token_doc.user_id)
        if not user or user.status != UserStatus.ACTIVE:
            raise AuthenticationError("user not found or inactive")

        app_id = token_doc.app_id
        scopes: list[str] | None = None
        if app_id:
            grant = await self._grant_repo.find_active_grant(user.id, app_id)
            if not grant:
                raise AuthenticationError("app access has been revoked")
            scopes = self.resolve_effective_scopes(grant)
            await self._touch_grant(user.id, app_id)

        svc_log.info(
            "device_auth_success",
            user_id=str(user.id),
            app_id=app_id,
            scopes=scopes,
        )
        access_token, refresh_token = await self._tokens.issue_tokens(
            user, "ext", app_id=app_id, scopes=scopes
        )
        return AuthResult(
            user=user,
            access_token=access_token,
            refresh_token=refresh_token,
            app_id=app_id,
        )

    # ── Token refresh ─────────────────────────────────────────────────────────

    async def refresh_device_tokens(self, refresh_token_str: str) -> AuthResult:
        """Rotate an app's token pair from a body-carried refresh token.

        If the refresh token carries an ``app_id`` claim, the grant must
        still be active and its effective scopes are re-read at every mint —
        scope changes propagate on the next refresh. Tokens without an
        ``app_id`` rotate unrestricted (legacy behavior).

        Raises:
            AuthenticationError: Token invalid/expired, user not found or
                inactive, or the app grant was revoked.
        """
        svc_log = log.bind(op="auth.device_refresh")

        claims = self._tokens.verify_token(refresh_token_str, token_type="refresh")

        user = await self._user_repo.find_by_id(ObjectId(claims.get("sub")))
        if not user or user.status != UserStatus.ACTIVE:
            svc_log.info(
                "device_refresh_failed",
                reason="user_not_found_or_inactive",
                user_id=claims.get("sub"),
            )
            raise AuthenticationError("invalid or expired refresh token")

        amr = claims.get("amr", ["pwd"])[0]
        app_id = claims.get("app_id")
        scopes: list[str] | None = None
        if app_id:
            grant = await self._grant_repo.find_active_grant(user.id, app_id)
            if not grant:
                raise AuthenticationError("app access has been revoked")
            scopes = self.resolve_effective_scopes(grant)
            await self._touch_grant(user.id, app_id)

        svc_log.info(
            "device_tokens_refreshed",
            user_id=str(user.id),
            amr=amr,
            app_id=app_id,
            scopes=scopes,
        )
        access_token, refresh_token = await self._tokens.issue_tokens(
            user, amr, app_id=app_id, scopes=scopes
        )
        return AuthResult(
            user=user,
            access_token=access_token,
            refresh_token=refresh_token,
            app_id=app_id,
        )

    # ── Revocation ────────────────────────────────────────────────────────────

    async def revoke_device_tokens(
        self, user_id: ObjectId, app_id: str | None = None
    ) -> int:
        """Invalidate device auth tokens for a user, optionally filtered by app_id.

        Returns the number of tokens deleted.
        """
        svc_log = log.bind(op="auth.device_revoke")

        count = await self._token_repo.delete_by_user(
            user_id, TOKEN_TYPE_DEVICE_AUTH, app_id=app_id
        )
        svc_log.info(
            "device_tokens_revoked",
            user_id=str(user_id),
            app_id=app_id,
            count=count,
        )
        return count

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _touch_grant(self, user_id: ObjectId, app_id: str) -> None:
        """Best-effort last_used_at bump — never fails the auth flow."""
        try:
            await self._grant_repo.touch_last_used(user_id, app_id)
        except Exception:
            log.info(
                "touch_last_used_failed",
                user_id=str(user_id),
                app_id=app_id,
            )
