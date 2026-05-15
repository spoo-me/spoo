"""
DeviceAuthService — device/extension authentication flow.

Handles the OAuth-like device auth flow used by browser extensions, CLIs,
and desktop apps.  Creates one-time auth codes, exchanges them for JWT
tokens, and revokes device tokens on app unlink.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bson import ObjectId

from errors import AuthenticationError
from infrastructure.crypto import hash_token
from infrastructure.logging import get_logger
from repositories.token_repository import TokenRepository
from repositories.user_repository import UserRepository
from schemas.models.app import AppEntry
from schemas.models.token import TOKEN_TYPE_DEVICE_AUTH
from schemas.models.user import UserStatus
from schemas.results import AuthResult
from services.token_factory import TokenFactory
from shared.generators import generate_secure_token

log = get_logger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

DEVICE_AUTH_EXPIRY_SECONDS = 300  # 5 minutes
APP_ID_MAX_LEN = 64


class DeviceAuthService:
    """Device/extension authentication flow.

    Args:
        user_repo:     Repository for the ``users`` collection.
        token_repo:    Repository for the ``verification-tokens`` collection.
        token_factory: JWT token generation.
        app_registry:  App registry loaded from apps.yaml at startup.
    """

    def __init__(
        self,
        user_repo: UserRepository,
        token_repo: TokenRepository,
        token_factory: TokenFactory,
        app_registry: dict[str, AppEntry] | None = None,
    ) -> None:
        self._user_repo = user_repo
        self._token_repo = token_repo
        self._tokens = token_factory
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
        """Return True if redirect_uri is empty, exact-matches an allowlist
        entry, or prefix-matches an entry ending with ``*``.

        The ``*`` suffix is used for OAuth clients (e.g. Raycast) that append
        a varying query string to a fixed redirect URL. Only exact or
        explicit-wildcard entries are accepted; implicit wildcards are not.
        """
        if not redirect_uri:
            return True
        for allowed in app.redirect_uris:
            if allowed.endswith("*"):
                if redirect_uri.startswith(allowed[:-1]):
                    return True
            elif redirect_uri == allowed:
                return True
        return False

    async def create_device_auth_code(
        self, user_id: ObjectId, email: str, app_id: str | None = None
    ) -> str:
        """Generate a one-time auth code for the device auth flow.

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
        }
        if app_id:
            token_data["app_id"] = app_id
        await self._token_repo.create(token_data)
        svc_log.info("device_auth_code_created", user_id=str(user_id), app_id=app_id)
        return raw_token

    async def exchange_device_code(self, code: str) -> AuthResult:
        """Exchange a one-time device auth code for JWT tokens.

        Raises:
            AuthenticationError: Code invalid, expired, or already used.
        """
        svc_log = log.bind(op="auth.device_exchange")

        token_hash = hash_token(code)
        token_doc = await self._token_repo.consume_by_hash(
            token_hash, TOKEN_TYPE_DEVICE_AUTH
        )
        if not token_doc:
            raise AuthenticationError("invalid or expired device auth code")

        user = await self._user_repo.find_by_id(token_doc.user_id)
        if not user or user.status != UserStatus.ACTIVE:
            raise AuthenticationError("user not found or inactive")

        app_id = token_doc.app_id
        svc_log.info("device_auth_success", user_id=str(user.id), app_id=app_id)
        access_token, refresh_token = self._tokens.issue_tokens(
            user, "ext", app_id=app_id
        )
        return AuthResult(
            user=user,
            access_token=access_token,
            refresh_token=refresh_token,
            app_id=app_id,
        )

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
