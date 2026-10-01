"""Unit tests for dependencies/auth.py — get_current_user and guards."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import jwt as pyjwt
import pytest
from bson import ObjectId

from dependencies.auth import (
    CurrentUser,
    check_api_key_scope,
    get_current_user,
    require_auth,
    require_verified_email,
)
from errors import AuthenticationError, EmailNotVerifiedError, ForbiddenError
from schemas.models.api_key import ApiKeyDoc

USER_OID = ObjectId("aaaaaaaaaaaaaaaaaaaaaaaa")
KEY_OID = ObjectId("cccccccccccccccccccccccc")
JWT_SECRET = "test-secret-key-at-least-32-chars!!"
JWT_ISSUER = "spoo.me"
JWT_AUDIENCE = "spoo.me.api"


# ── Helpers ───────────────────────────────────────────────────────────────────


def make_jwt_settings():
    from config import JWTSettings

    return JWTSettings(
        jwt_issuer=JWT_ISSUER,
        jwt_audience=JWT_AUDIENCE,
        access_token_ttl_seconds=900,
        refresh_token_ttl_seconds=2592000,
        jwt_secret=JWT_SECRET,
        jwt_private_key="",
        jwt_public_key="",
    )


def make_settings():
    s = MagicMock()
    s.jwt = make_jwt_settings()
    return s


def make_request(auth_header: str = "", cookies: dict | None = None):
    req = MagicMock()
    req.headers.get = lambda key, default="": (
        auth_header if key == "Authorization" else default
    )
    req.cookies.get = lambda key, default=None: (cookies or {}).get(key, default)
    return req


def make_key_doc(
    revoked: bool = False, expires_at=None, scopes=None, last_used_at=None
):
    return ApiKeyDoc.from_mongo(
        {
            "_id": KEY_OID,
            "user_id": USER_OID,
            "token_prefix": "abcd1234",
            "token_hash": "x" * 64,
            "name": "Test Key",
            "scopes": scopes or ["urls:read"],
            "revoked": revoked,
            "expires_at": expires_at,
            "created_at": datetime.now(timezone.utc),
            "last_used_at": last_used_at,
        }
    )


def make_jwt_token(
    token_type: str = "access",
    ttl_seconds: int = 900,
    email: str | None = None,
):
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(USER_OID),
        "email_verified": True,
        "type": token_type,
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "iat": now,
        "exp": now + timedelta(seconds=ttl_seconds),
    }
    if email is not None:
        payload["email"] = email
    return pyjwt.encode(payload, JWT_SECRET, algorithm="HS256")


# ── TestGetCurrentUser ────────────────────────────────────────────────────────


class TestGetCurrentUser:
    @pytest.mark.asyncio
    async def test_no_auth_returns_none(self):
        req = make_request()
        with patch("dependencies.auth.get_settings", return_value=make_settings()):
            result = await get_current_user(req, db=MagicMock())
        assert result is None

    @pytest.mark.asyncio
    async def test_api_key_valid_returns_current_user(self):
        key_doc = make_key_doc()
        user_mock = MagicMock(email_verified=True, email="Owner@Example.com")

        with (
            patch("dependencies.auth.get_settings", return_value=make_settings()),
            patch("dependencies.auth.ApiKeyRepository") as MockKeyRepo,
            patch("dependencies.auth.UserRepository") as MockUserRepo,
        ):
            MockKeyRepo.return_value.find_by_hash = AsyncMock(return_value=key_doc)
            MockUserRepo.return_value.find_by_id = AsyncMock(return_value=user_mock)

            req = make_request(auth_header="Bearer spoo_testrawtoken123")
            result = await get_current_user(req, db=MagicMock())

        assert result is not None
        assert result.user_id == USER_OID
        assert result.api_key_doc == key_doc
        assert result.email_verified is True
        # Email comes from the owning UserDoc (already fetched), lowercased.
        assert result.email == "owner@example.com"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["PENDING_DELETION", "ERASING"])
    async def test_api_key_blocked_while_account_deletion_pending(self, status):
        """A valid key on a doomed account raises the same error the login
        gate uses — keys must go dark for the whole grace window."""
        from errors import AccountPendingDeletionError
        from schemas.models.user import UserStatus

        key_doc = make_key_doc()
        user_mock = MagicMock(
            email_verified=True,
            email="owner@example.com",
            status=UserStatus(status),
        )

        with (
            patch("dependencies.auth.get_settings", return_value=make_settings()),
            patch("dependencies.auth.ApiKeyRepository") as MockKeyRepo,
            patch("dependencies.auth.UserRepository") as MockUserRepo,
        ):
            MockKeyRepo.return_value.find_by_hash = AsyncMock(return_value=key_doc)
            MockUserRepo.return_value.find_by_id = AsyncMock(return_value=user_mock)

            req = make_request(auth_header="Bearer spoo_testrawtoken123")
            with pytest.raises(AccountPendingDeletionError) as exc_info:
                await get_current_user(req, db=MagicMock())

        assert exc_info.value.error_code == "ACCOUNT_PENDING_DELETION"
        # The last-used stamp never ran — the request was rejected.
        MockKeyRepo.return_value.touch_last_used.assert_not_called()

    @pytest.mark.asyncio
    async def test_api_key_works_again_after_restore(self):
        """ACTIVE (restored) accounts authenticate normally."""
        from schemas.models.user import UserStatus

        key_doc = make_key_doc()
        user_mock = MagicMock(
            email_verified=True,
            email="owner@example.com",
            status=UserStatus.ACTIVE,
        )

        with (
            patch("dependencies.auth.get_settings", return_value=make_settings()),
            patch("dependencies.auth.ApiKeyRepository") as MockKeyRepo,
            patch("dependencies.auth.UserRepository") as MockUserRepo,
        ):
            MockKeyRepo.return_value.find_by_hash = AsyncMock(return_value=key_doc)
            MockUserRepo.return_value.find_by_id = AsyncMock(return_value=user_mock)

            req = make_request(auth_header="Bearer spoo_testrawtoken123")
            result = await get_current_user(req, db=MagicMock())

        assert result is not None
        assert result.user_id == USER_OID

    @pytest.mark.asyncio
    async def test_api_key_inactive_account_not_gated(self):
        """INACTIVE is deliberately NOT blocked here — parity with login."""
        from schemas.models.user import UserStatus

        key_doc = make_key_doc()
        user_mock = MagicMock(
            email_verified=True,
            email="owner@example.com",
            status=UserStatus.INACTIVE,
        )

        with (
            patch("dependencies.auth.get_settings", return_value=make_settings()),
            patch("dependencies.auth.ApiKeyRepository") as MockKeyRepo,
            patch("dependencies.auth.UserRepository") as MockUserRepo,
        ):
            MockKeyRepo.return_value.find_by_hash = AsyncMock(return_value=key_doc)
            MockUserRepo.return_value.find_by_id = AsyncMock(return_value=user_mock)

            req = make_request(auth_header="Bearer spoo_testrawtoken123")
            result = await get_current_user(req, db=MagicMock())

        assert result is not None

    @pytest.mark.asyncio
    async def test_api_key_revoked_returns_none(self):
        key_doc = make_key_doc(revoked=True)

        with (
            patch("dependencies.auth.get_settings", return_value=make_settings()),
            patch("dependencies.auth.ApiKeyRepository") as MockKeyRepo,
        ):
            MockKeyRepo.return_value.find_by_hash = AsyncMock(return_value=key_doc)

            req = make_request(auth_header="Bearer spoo_testrawtoken123")
            result = await get_current_user(req, db=MagicMock())

        assert result is None

    @pytest.mark.asyncio
    async def test_api_key_expired_returns_none(self):
        expired = datetime(2020, 1, 1, tzinfo=timezone.utc)
        key_doc = make_key_doc(expires_at=expired)

        with (
            patch("dependencies.auth.get_settings", return_value=make_settings()),
            patch("dependencies.auth.ApiKeyRepository") as MockKeyRepo,
        ):
            MockKeyRepo.return_value.find_by_hash = AsyncMock(return_value=key_doc)

            req = make_request(auth_header="Bearer spoo_testrawtoken123")
            result = await get_current_user(req, db=MagicMock())

        assert result is None

    @pytest.mark.asyncio
    async def test_api_key_not_found_returns_none(self):
        with (
            patch("dependencies.auth.get_settings", return_value=make_settings()),
            patch("dependencies.auth.ApiKeyRepository") as MockKeyRepo,
        ):
            MockKeyRepo.return_value.find_by_hash = AsyncMock(return_value=None)

            req = make_request(auth_header="Bearer spoo_notfound")
            result = await get_current_user(req, db=MagicMock())

        assert result is None

    @pytest.mark.asyncio
    async def test_api_key_repo_error_returns_none(self):
        with (
            patch("dependencies.auth.get_settings", return_value=make_settings()),
            patch("dependencies.auth.ApiKeyRepository") as MockKeyRepo,
        ):
            MockKeyRepo.return_value.find_by_hash = AsyncMock(
                side_effect=RuntimeError("db error")
            )

            req = make_request(auth_header="Bearer spoo_sometoken")
            result = await get_current_user(req, db=MagicMock())

        assert result is None

    @pytest.mark.asyncio
    async def test_jwt_valid_returns_current_user(self):
        token = make_jwt_token()
        req = make_request(auth_header=f"Bearer {token}")

        with patch("dependencies.auth.get_settings", return_value=make_settings()):
            result = await get_current_user(req, db=MagicMock())

        assert result is not None
        assert result.user_id == USER_OID
        assert result.email_verified is True
        assert result.api_key_doc is None

    @pytest.mark.asyncio
    async def test_jwt_email_claim_populates_current_user_lowercased(self):
        token = make_jwt_token(email="Alice@Example.COM")
        req = make_request(auth_header=f"Bearer {token}")

        with patch("dependencies.auth.get_settings", return_value=make_settings()):
            result = await get_current_user(req, db=MagicMock())

        assert result is not None
        assert result.email == "alice@example.com"

    @pytest.mark.asyncio
    async def test_jwt_without_email_claim_yields_none_email(self):
        # Old access tokens minted before the "email" claim existed must
        # still authenticate — email is simply None, never an error.
        token = make_jwt_token()
        req = make_request(auth_header=f"Bearer {token}")

        with patch("dependencies.auth.get_settings", return_value=make_settings()):
            result = await get_current_user(req, db=MagicMock())

        assert result is not None
        assert result.email is None

    @pytest.mark.asyncio
    async def test_jwt_blank_email_claim_yields_none_email(self):
        token = make_jwt_token(email="   ")
        req = make_request(auth_header=f"Bearer {token}")

        with patch("dependencies.auth.get_settings", return_value=make_settings()):
            result = await get_current_user(req, db=MagicMock())

        assert result is not None
        assert result.email is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_email", [42, ["alice@example.com"]])
    async def test_jwt_non_string_email_claim_yields_none_email(self, bad_email):
        # A token whose "email" claim is not a string (int, list, …) must
        # still authenticate — the claim parses to None, never an error.
        token = make_jwt_token(email=bad_email)
        req = make_request(auth_header=f"Bearer {token}")

        with patch("dependencies.auth.get_settings", return_value=make_settings()):
            result = await get_current_user(req, db=MagicMock())

        assert result is not None
        assert result.user_id == USER_OID
        assert result.email is None

    @pytest.mark.asyncio
    async def test_token_factory_round_trip_populates_email(self):
        # Mint with the real TokenFactory → resolve via get_current_user:
        # the email claim survives the round trip and is lowercased.
        from schemas.models.user import UserDoc
        from services.token_factory import TokenFactory

        user_doc = UserDoc.from_mongo(
            {
                "_id": USER_OID,
                "email": "Round.Trip@Example.COM",
                "email_verified": True,
                "user_name": "Round Trip",
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
        )
        token = TokenFactory(make_jwt_settings()).generate_access_token(
            user_doc, amr="pwd"
        )
        req = make_request(auth_header=f"Bearer {token}")

        with patch("dependencies.auth.get_settings", return_value=make_settings()):
            result = await get_current_user(req, db=MagicMock())

        assert result is not None
        assert result.user_id == USER_OID
        assert result.email == "round.trip@example.com"

    @pytest.mark.asyncio
    async def test_jwt_refresh_token_rejected(self):
        token = make_jwt_token(token_type="refresh")
        req = make_request(auth_header=f"Bearer {token}")

        with patch("dependencies.auth.get_settings", return_value=make_settings()):
            result = await get_current_user(req, db=MagicMock())

        assert result is None

    @pytest.mark.asyncio
    async def test_jwt_invalid_returns_none(self):
        req = make_request(auth_header="Bearer not.a.valid.jwt")

        with patch("dependencies.auth.get_settings", return_value=make_settings()):
            result = await get_current_user(req, db=MagicMock())

        assert result is None

    @pytest.mark.asyncio
    async def test_jwt_from_cookie_returns_current_user(self):
        token = make_jwt_token()
        req = make_request(cookies={"access_token": token})

        with patch("dependencies.auth.get_settings", return_value=make_settings()):
            result = await get_current_user(req, db=MagicMock())

        assert result is not None
        assert result.user_id == USER_OID


# ── TestRequireAuth ───────────────────────────────────────────────────────────


class TestRequireAuth:
    @pytest.mark.asyncio
    async def test_raises_when_no_user(self):
        with pytest.raises(AuthenticationError):
            await require_auth(user=None)

    @pytest.mark.asyncio
    async def test_passes_through_authenticated_user(self):
        user = CurrentUser(user_id=USER_OID, email_verified=True)
        result = await require_auth(user=user)
        assert result is user


# ── TestRequireVerifiedEmail ──────────────────────────────────────────────────


class TestRequireVerifiedEmail:
    @pytest.mark.asyncio
    async def test_raises_when_email_not_verified(self):
        user = CurrentUser(user_id=USER_OID, email_verified=False)
        with pytest.raises(EmailNotVerifiedError):
            await require_verified_email(user=user)

    @pytest.mark.asyncio
    async def test_passes_through_verified_user(self):
        user = CurrentUser(user_id=USER_OID, email_verified=True)
        result = await require_verified_email(user=user)
        assert result is user


# ── TestCheckApiKeyScope ──────────────────────────────────────────────────────


class TestCheckApiKeyScope:
    def test_raises_when_scope_missing(self):
        key_doc = make_key_doc(scopes=["urls:read"])
        user = CurrentUser(user_id=USER_OID, email_verified=True, api_key_doc=key_doc)
        with pytest.raises(ForbiddenError):
            check_api_key_scope(user, {"urls:manage"})

    def test_passes_when_scope_present(self):
        key_doc = make_key_doc(scopes=["urls:manage"])
        user = CurrentUser(user_id=USER_OID, email_verified=True, api_key_doc=key_doc)
        check_api_key_scope(user, {"urls:manage"})  # no raise

    def test_passes_when_scope_overlaps(self):
        key_doc = make_key_doc(scopes=["urls:read", "urls:manage"])
        user = CurrentUser(user_id=USER_OID, email_verified=True, api_key_doc=key_doc)
        check_api_key_scope(user, {"urls:manage", "admin:all"})  # intersection exists

    def test_jwt_user_bypasses_scope_check(self):
        user = CurrentUser(user_id=USER_OID, email_verified=True)  # no api_key_doc
        check_api_key_scope(user, {"url:write"})  # no raise

    def test_anonymous_bypasses_scope_check(self):
        check_api_key_scope(None, {"url:write"})  # no raise


# ── Scoped app tokens (device auth) ───────────────────────────────────────────


def make_scoped_jwt(scopes=None, app_id="spoo-cli", ttl_seconds: int = 900):
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(USER_OID),
        "email_verified": True,
        "type": "access",
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "iat": now,
        "exp": now + timedelta(seconds=ttl_seconds),
        "amr": ["ext"],
    }
    if scopes is not None:
        payload["scp"] = scopes
    if app_id is not None:
        payload["app_id"] = app_id
    return pyjwt.encode(payload, JWT_SECRET, algorithm="HS256")


class TestScopedTokenParsing:
    @pytest.mark.asyncio
    async def test_scp_and_app_id_populate_current_user(self):
        token = make_scoped_jwt(scopes=["urls:read", "stats:read"])
        req = make_request(auth_header=f"Bearer {token}")
        with patch("dependencies.auth.get_settings", return_value=make_settings()):
            user = await get_current_user(req, db=MagicMock())
        assert user is not None
        assert user.scopes == ["urls:read", "stats:read"]
        assert user.app_id == "spoo-cli"

    @pytest.mark.asyncio
    async def test_session_token_has_no_scopes(self):
        token = make_jwt_token()
        req = make_request(auth_header=f"Bearer {token}")
        with patch("dependencies.auth.get_settings", return_value=make_settings()):
            user = await get_current_user(req, db=MagicMock())
        assert user is not None
        assert user.scopes is None
        assert user.app_id is None

    @pytest.mark.asyncio
    async def test_malformed_scp_fails_closed(self):
        """A non-list scp claim yields an empty scope set, not unrestricted."""
        token = make_scoped_jwt(scopes="urls:read")  # string, not list
        req = make_request(auth_header=f"Bearer {token}")
        with patch("dependencies.auth.get_settings", return_value=make_settings()):
            user = await get_current_user(req, db=MagicMock())
        assert user is not None
        assert user.scopes == []


class TestCredentialScopeCheck:
    def _scoped_user(self, scopes):
        return CurrentUser(
            user_id=USER_OID, email_verified=True, scopes=scopes, app_id="spoo-cli"
        )

    def test_token_scopes_pass_when_intersecting(self):
        from dependencies.auth import URL_READ_SCOPES, check_credential_scopes

        check_credential_scopes(self._scoped_user(["urls:read"]), URL_READ_SCOPES)

    def test_token_scopes_403_when_disjoint(self):
        from dependencies.auth import URL_READ_SCOPES, check_credential_scopes

        with pytest.raises(ForbiddenError):
            check_credential_scopes(
                self._scoped_user(["shorten:create"]), URL_READ_SCOPES
            )

    def test_empty_token_scopes_403(self):
        from dependencies.auth import URL_READ_SCOPES, check_credential_scopes

        with pytest.raises(ForbiddenError):
            check_credential_scopes(self._scoped_user([]), URL_READ_SCOPES)

    def test_session_user_unrestricted(self):
        from dependencies.auth import URL_READ_SCOPES, check_credential_scopes

        check_credential_scopes(
            CurrentUser(user_id=USER_OID, email_verified=True), URL_READ_SCOPES
        )

    def test_api_key_scopes_still_checked(self):
        from dependencies.auth import URL_READ_SCOPES, check_credential_scopes

        key_user = CurrentUser(
            user_id=USER_OID,
            email_verified=True,
            api_key_doc=make_key_doc(scopes=["shorten:create"]),
        )
        with pytest.raises(ForbiddenError):
            check_credential_scopes(key_user, URL_READ_SCOPES)

    def test_check_api_key_scope_alias_preserved(self):
        from dependencies.auth import check_api_key_scope, check_credential_scopes

        assert check_api_key_scope is check_credential_scopes


class TestRequireJwtRejectsScopedTokens:
    @pytest.mark.asyncio
    async def test_scoped_token_403(self):
        from dependencies.auth import require_jwt

        user = CurrentUser(
            user_id=USER_OID,
            email_verified=True,
            scopes=["urls:read"],
            app_id="spoo-cli",
        )
        with pytest.raises(ForbiddenError, match="interactive session"):
            await require_jwt(user)

    @pytest.mark.asyncio
    async def test_legacy_app_token_403(self):
        """Legacy grant token: app_id set, scopes None — still delegated."""
        from dependencies.auth import require_jwt

        user = CurrentUser(
            user_id=USER_OID, email_verified=True, scopes=None, app_id="spoo-cli"
        )
        with pytest.raises(ForbiddenError, match="interactive session"):
            await require_jwt(user)

    @pytest.mark.asyncio
    async def test_session_user_passes(self):
        from dependencies.auth import require_jwt

        user = CurrentUser(user_id=USER_OID, email_verified=True)
        assert await require_jwt(user) is user


class TestRequireKeysAccess:
    @pytest.mark.asyncio
    async def test_interactive_session_passes(self):
        from dependencies.auth import require_keys_access

        user = CurrentUser(user_id=USER_OID, email_verified=True)
        assert await require_keys_access(user) is user

    @pytest.mark.asyncio
    async def test_app_token_with_keys_manage_passes(self):
        from dependencies.auth import require_keys_access

        user = CurrentUser(
            user_id=USER_OID,
            email_verified=True,
            scopes=["keys:manage"],
            app_id="spoo-cli",
        )
        assert await require_keys_access(user) is user

    @pytest.mark.asyncio
    async def test_app_token_without_keys_manage_403(self):
        from dependencies.auth import require_keys_access

        user = CurrentUser(
            user_id=USER_OID,
            email_verified=True,
            scopes=["shorten:create", "admin:all"],
            app_id="spoo-cli",
        )
        with pytest.raises(ForbiddenError):
            await require_keys_access(user)

    @pytest.mark.asyncio
    async def test_api_key_403_even_with_admin_all(self):
        """API keys can never manage keys — keys:manage is uncreatable on them."""
        from dependencies.auth import require_keys_access

        user = CurrentUser(
            user_id=USER_OID,
            email_verified=True,
            api_key_doc=make_key_doc(scopes=["admin:all"]),
        )
        with pytest.raises(ForbiddenError):
            await require_keys_access(user)

    @pytest.mark.asyncio
    async def test_legacy_app_token_403(self):
        """Legacy grant token (app_id, no scp) predates keys:manage — denied."""
        from dependencies.auth import require_keys_access

        user = CurrentUser(
            user_id=USER_OID, email_verified=True, scopes=None, app_id="spoo-cli"
        )
        with pytest.raises(ForbiddenError):
            await require_keys_access(user)


# ── TestApiKeyHygiene ────────────────────────────────────────────────────────


class TestApiKeyHygiene:
    """last_used_at stamping (debounced, best-effort) and failed-auth logging."""

    def _mocks(self):
        settings_patch = patch(
            "dependencies.auth.get_settings", return_value=make_settings()
        )
        key_repo_patch = patch("dependencies.auth.ApiKeyRepository")
        user_repo_patch = patch("dependencies.auth.UserRepository")
        return settings_patch, key_repo_patch, user_repo_patch

    async def _auth(self, key_doc, MockKeyRepo, MockUserRepo):
        MockKeyRepo.return_value.find_by_hash = AsyncMock(return_value=key_doc)
        MockKeyRepo.return_value.touch_last_used = AsyncMock()
        MockUserRepo.return_value.find_by_id = AsyncMock(
            return_value=MagicMock(email_verified=True, email="o@e.com")
        )
        req = make_request(auth_header="Bearer spoo_testrawtoken123")
        return await get_current_user(req, db=MagicMock())

    @pytest.mark.asyncio
    async def test_touch_called_when_never_used(self):
        key_doc = make_key_doc(last_used_at=None)
        sp, kp, up = self._mocks()
        with sp, kp as MockKeyRepo, up as MockUserRepo:
            result = await self._auth(key_doc, MockKeyRepo, MockUserRepo)
            MockKeyRepo.return_value.touch_last_used.assert_awaited_once_with(KEY_OID)
        assert result is not None

    @pytest.mark.asyncio
    async def test_touch_debounced_when_recently_used(self):
        recent = datetime.now(timezone.utc) - timedelta(minutes=5)
        key_doc = make_key_doc(last_used_at=recent)
        sp, kp, up = self._mocks()
        with sp, kp as MockKeyRepo, up as MockUserRepo:
            result = await self._auth(key_doc, MockKeyRepo, MockUserRepo)
            MockKeyRepo.return_value.touch_last_used.assert_not_awaited()
        assert result is not None

    @pytest.mark.asyncio
    async def test_touch_called_when_stale(self):
        stale = datetime.now(timezone.utc) - timedelta(hours=2)
        key_doc = make_key_doc(last_used_at=stale)
        sp, kp, up = self._mocks()
        with sp, kp as MockKeyRepo, up as MockUserRepo:
            result = await self._auth(key_doc, MockKeyRepo, MockUserRepo)
            MockKeyRepo.return_value.touch_last_used.assert_awaited_once_with(KEY_OID)
        assert result is not None

    @pytest.mark.asyncio
    async def test_naive_last_used_treated_as_utc(self):
        # Mongo round-trips datetimes as naive UTC; a recent naive stamp
        # must still debounce instead of raising on aware/naive subtraction.
        recent_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        key_doc = make_key_doc(last_used_at=recent_naive)
        sp, kp, up = self._mocks()
        with sp, kp as MockKeyRepo, up as MockUserRepo:
            result = await self._auth(key_doc, MockKeyRepo, MockUserRepo)
            MockKeyRepo.return_value.touch_last_used.assert_not_awaited()
        assert result is not None

    @pytest.mark.asyncio
    async def test_touch_failure_does_not_fail_auth(self):
        key_doc = make_key_doc(last_used_at=None)
        sp, kp, up = self._mocks()
        with sp, kp as MockKeyRepo, up as MockUserRepo:
            MockKeyRepo.return_value.find_by_hash = AsyncMock(return_value=key_doc)
            MockKeyRepo.return_value.touch_last_used = AsyncMock(
                side_effect=RuntimeError("mongo down")
            )
            MockUserRepo.return_value.find_by_id = AsyncMock(
                return_value=MagicMock(email_verified=True, email="o@e.com")
            )
            req = make_request(auth_header="Bearer spoo_testrawtoken123")
            result = await get_current_user(req, db=MagicMock())
        assert result is not None
        assert result.user_id == USER_OID

    @pytest.mark.asyncio
    async def test_unknown_key_logs_prefix_only(self):
        with (
            patch("dependencies.auth.get_settings", return_value=make_settings()),
            patch("dependencies.auth.ApiKeyRepository") as MockKeyRepo,
            patch("dependencies.auth.log") as mock_log,
        ):
            MockKeyRepo.return_value.find_by_hash = AsyncMock(return_value=None)
            req = make_request(auth_header="Bearer spoo_notfoundtoken99")
            result = await get_current_user(req, db=MagicMock())

        assert result is None
        mock_log.warning.assert_called_once_with(
            "api_key_auth_failed", reason="unknown", key_prefix="notfound"
        )

    @pytest.mark.asyncio
    async def test_revoked_key_logs_reason(self):
        key_doc = make_key_doc(revoked=True)
        with (
            patch("dependencies.auth.get_settings", return_value=make_settings()),
            patch("dependencies.auth.ApiKeyRepository") as MockKeyRepo,
            patch("dependencies.auth.log") as mock_log,
        ):
            MockKeyRepo.return_value.find_by_hash = AsyncMock(return_value=key_doc)
            req = make_request(auth_header="Bearer spoo_testrawtoken123")
            result = await get_current_user(req, db=MagicMock())

        assert result is None
        mock_log.warning.assert_called_once_with(
            "api_key_auth_failed",
            reason="revoked",
            key_prefix="abcd1234",
            key_id=str(KEY_OID),
            user_id=str(USER_OID),
        )

    @pytest.mark.asyncio
    async def test_expired_key_logs_reason(self):
        expired = datetime.now(timezone.utc) - timedelta(days=1)
        key_doc = make_key_doc(expires_at=expired)
        with (
            patch("dependencies.auth.get_settings", return_value=make_settings()),
            patch("dependencies.auth.ApiKeyRepository") as MockKeyRepo,
            patch("dependencies.auth.log") as mock_log,
        ):
            MockKeyRepo.return_value.find_by_hash = AsyncMock(return_value=key_doc)
            req = make_request(auth_header="Bearer spoo_testrawtoken123")
            result = await get_current_user(req, db=MagicMock())

        assert result is None
        mock_log.warning.assert_called_once_with(
            "api_key_auth_failed",
            reason="expired",
            key_prefix="abcd1234",
            key_id=str(KEY_OID),
            user_id=str(USER_OID),
        )


class TestPausedApiKey:
    @pytest.mark.asyncio
    async def test_paused_key_is_denied(self):
        key = make_key_doc().model_copy(update={"paused_by_limit": True})
        req = make_request(auth_header="Bearer spoo_rawtoken")
        db = MagicMock()
        with (
            patch("dependencies.auth.get_settings", return_value=make_settings()),
            patch("dependencies.auth.ApiKeyRepository") as repo_cls,
        ):
            repo_cls.return_value.find_by_hash = AsyncMock(return_value=key)
            assert await get_current_user(req, db=db) is None
