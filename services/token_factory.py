"""
TokenFactory — JWT token generation and verification.

Stateless helper composed inside AuthService. Knows only about JWTSettings
and the PyJWT library. No database or email I/O happens here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import jwt as pyjwt

from config import JWTSettings
from errors import AuthenticationError
from schemas.models.user import UserDoc


class TokenFactory:
    """Issues and verifies JWT access and refresh tokens.

    Args:
        settings: JWT configuration (issuer, audience, keys, TTLs).
        plan_of:  Async lookup of the user's plan name for the ``plan``
                  claim. Optional; ``"free"`` when absent or failing.
    """

    def __init__(
        self,
        settings: JWTSettings,
        plan_of: Callable[[Any], Awaitable[str]] | None = None,
    ) -> None:
        self._settings = settings
        self._plan_of = plan_of

    # ── Key / algorithm helpers ───────────────────────────────────────────────

    def _algorithm(self) -> str:
        return "RS256" if self._settings.use_rs256 else "HS256"

    def _signing_key(self) -> str:
        return (
            self._settings.jwt_private_key
            if self._settings.use_rs256
            else self._settings.jwt_secret
        )

    def _verification_key(self) -> str:
        return (
            self._settings.jwt_public_key
            if self._settings.use_rs256
            else self._settings.jwt_secret
        )

    # ── Token generation ──────────────────────────────────────────────────────

    def generate_access_token(
        self,
        user: UserDoc,
        *,
        amr: str,
        app_id: str | None = None,
        scopes: list[str] | None = None,
        plan: str = "free",
    ) -> str:
        """Issue a signed JWT access token for *user*.

        Claims:
            iss            — issuer (from settings)
            aud            — audience (from settings)
            sub            — user ObjectId as string
            iat            — issued-at (UTC epoch seconds)
            exp            — expiry (iat + access_token_ttl_seconds)
            amr            — authentication method reference list, e.g. ["pwd"]
            email          — lowercased email from the user document (consumed
                             by feature-flag email allowlists via CurrentUser)
            email_verified — bool from the user document
            plan           — plan name at issue time; a hint for the client
                             and the resolver's last fallback, never authority
            app_id         — connected-app id (device auth flow only)
            scp            — granted scope slugs (device auth flow only;
                             omitted entirely for unrestricted tokens)
        """
        now = int(datetime.now(timezone.utc).timestamp())
        payload: dict[str, Any] = {
            "iss": self._settings.jwt_issuer,
            "aud": self._settings.jwt_audience,
            "sub": str(user.id),
            "iat": now,
            "exp": now + self._settings.access_token_ttl_seconds,
            "amr": [amr],
            "email": user.email.lower(),
            "email_verified": user.email_verified,
            "plan": plan,
        }
        if app_id:
            payload["app_id"] = app_id
        if scopes is not None:
            payload["scp"] = list(scopes)
        return pyjwt.encode(payload, self._signing_key(), algorithm=self._algorithm())

    def generate_refresh_token(
        self, user: UserDoc, *, amr: str, app_id: str | None = None
    ) -> str:
        """Issue a signed JWT refresh token for *user*.

        Identical to the access token but includes ``"type": "refresh"`` and
        uses ``refresh_token_ttl_seconds`` for the expiry window.

        When *app_id* is provided (device auth flow), it is embedded as a claim
        so the refresh endpoint can enforce grant checks on a per-app basis.
        """
        now = int(datetime.now(timezone.utc).timestamp())
        payload: dict[str, Any] = {
            "iss": self._settings.jwt_issuer,
            "aud": self._settings.jwt_audience,
            "sub": str(user.id),
            "iat": now,
            "exp": now + self._settings.refresh_token_ttl_seconds,
            "amr": [amr],
            "email_verified": user.email_verified,
            "type": "refresh",
        }
        if app_id:
            payload["app_id"] = app_id
        return pyjwt.encode(payload, self._signing_key(), algorithm=self._algorithm())

    async def issue_tokens(
        self,
        user: UserDoc,
        amr: str,
        *,
        app_id: str | None = None,
        scopes: list[str] | None = None,
    ) -> tuple[str, str]:
        """Issue an access + refresh token pair for *user*.

        ``scopes`` land on the access token only — the refresh token carries
        just ``app_id`` so scope changes on the grant propagate at the next
        refresh instead of being frozen into the long-lived credential.

        Returns:
            (access_token, refresh_token)
        """
        plan = "free"
        if self._plan_of is not None:
            try:
                plan = await self._plan_of(user.id)
            except Exception:
                plan = "free"
        return (
            self.generate_access_token(
                user, amr=amr, app_id=app_id, scopes=scopes, plan=plan
            ),
            self.generate_refresh_token(user, amr=amr, app_id=app_id),
        )

    # ── Token verification ────────────────────────────────────────────────────

    def verify_token(self, token: str, *, token_type: str) -> dict[str, Any]:
        """Decode and validate *token*.

        Args:
            token:      Raw JWT string.
            token_type: ``"access"`` or ``"refresh"``. When ``"refresh"``,
                        the payload must contain ``"type": "refresh"``.

        Returns:
            The decoded payload dict.

        Raises:
            AuthenticationError: On any decode / validation failure.
        """
        try:
            payload: dict[str, Any] = pyjwt.decode(
                token,
                self._verification_key(),
                algorithms=[self._algorithm()],
                audience=self._settings.jwt_audience,
                issuer=self._settings.jwt_issuer,
            )
        except pyjwt.PyJWTError as exc:
            raise AuthenticationError(f"Invalid token: {exc}") from exc

        if token_type == "refresh" and payload.get("type") != "refresh":
            raise AuthenticationError(
                "Token is not a refresh token — 'type' field missing or incorrect."
            )

        return payload
