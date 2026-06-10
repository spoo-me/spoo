"""
OAuthService — OAuth flow orchestration.

Handles the four callback flows (existing OAuth user login, new user creation,
email collision + auto-link, account linking), provider management (link/unlink),
and provider listing.  Framework-agnostic: no FastAPI imports

The route layer is responsible for:
    - OAuth state validation (verify_oauth_state)
    - Authlib token exchange (client.authorize_access_token())
    - Provider-specific user-info fetch (strategy.fetch_user_info())
    - Redirecting with JWT cookies

This service receives the already-extracted ProviderInfo model and handles
all business logic from there.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from bson import ObjectId

from errors import AppError, ConflictError, NotFoundError, ValidationError
from infrastructure.email.protocol import EmailProvider
from infrastructure.logging import get_logger
from repositories.user_repository import UserRepository
from schemas.dto.responses.auth import OAuthProviderDetail
from schemas.models.user import (
    AuthProviderEntry,
    OAuthAction,
    ProfilePicture,
    ProviderInfo,
    ProviderProfile,
    UserDoc,
    UserPlan,
    UserStatus,
)
from schemas.results import AuthResult
from services.token_factory import TokenFactory

log = get_logger(__name__)


class OAuthService:
    """OAuth flow orchestration service.

    Args:
        user_repo:     Repository for the ``users`` collection.
        token_factory: JWT token generation (issues access + refresh pairs).
        email:         Email provider for welcome emails.
    """

    def __init__(
        self,
        user_repo: UserRepository,
        token_factory: TokenFactory,
        email: EmailProvider,
    ) -> None:
        self._user_repo = user_repo
        self._token_factory = token_factory
        self._email = email

    # ── Private helpers ───────────────────────────────────────────────────────

    def _can_auto_link(
        self, existing_user: UserDoc, provider_info: ProviderInfo, provider_key: str
    ) -> bool:
        """True only if provider email is verified, matches user email, and
        isn't already linked.

        Preserves exact logic from utils/oauth_utils.can_auto_link_accounts().
        """
        if not provider_info.email_verified:
            return False
        if existing_user.email.lower() != (provider_info.email or "").lower():
            return False
        for entry in existing_user.auth_providers:
            if entry.provider == provider_key:
                return False
        return True

    async def _link_provider(
        self,
        user_id: ObjectId,
        provider_info: ProviderInfo,
        provider: str,
    ) -> bool:
        """Append an OAuth provider entry to a user's auth_providers array.

        Also updates pfp if the provider has a picture, and sets
        email_verified=True if the provider verified the email.

        Returns:
            True if the document was matched and updated.
        """
        now = datetime.now(timezone.utc)
        provider_entry = AuthProviderEntry(
            provider=provider,
            provider_user_id=provider_info.provider_user_id,
            email=provider_info.email,
            email_verified=provider_info.email_verified,
            profile=ProviderProfile(
                name=provider_info.name,
                picture=provider_info.picture,
            ),
            linked_at=now,
        )

        set_fields: dict[str, Any] = {"updated_at": now, "last_login_at": now}

        if provider_info.picture:
            set_fields["pfp"] = {
                "url": provider_info.picture,
                "source": provider,
                "last_updated": now,
            }

        if provider_info.email_verified:
            set_fields["email_verified"] = True

        return await self._user_repo.update(
            user_id,
            {
                "$push": {"auth_providers": provider_entry.model_dump()},
                "$set": set_fields,
            },
        )

    async def _create_oauth_user(
        self,
        provider_info: ProviderInfo,
        provider: str,
        signup_ip: str | None = None,
    ) -> ObjectId:
        """Create a brand-new user from OAuth provider information.

        Returns:
            The new user's ObjectId.

        Raises:
            AppError: On DB insertion failure.
        """
        now = datetime.now(timezone.utc)
        user_name = provider_info.name or provider_info.email.split("@")[0]
        pfp = (
            ProfilePicture(
                url=provider_info.picture,
                source=provider,
                last_updated=now,
            )
            if provider_info.picture
            else None
        )

        user_doc = UserDoc(
            email=provider_info.email,
            email_verified=provider_info.email_verified,
            user_name=user_name,
            pfp=pfp,
            password_hash=None,
            password_set=False,
            auth_providers=[
                AuthProviderEntry(
                    provider=provider,
                    provider_user_id=provider_info.provider_user_id,
                    email=provider_info.email,
                    email_verified=provider_info.email_verified,
                    profile=ProviderProfile(
                        name=provider_info.name,
                        picture=provider_info.picture,
                    ),
                    linked_at=now,
                )
            ],
            plan=UserPlan.FREE,
            signup_ip=signup_ip,
            created_at=now,
            updated_at=now,
            last_login_at=now,
            status=UserStatus.ACTIVE,
        )
        user_data = user_doc.model_dump(by_alias=True, exclude={"id"})

        try:
            return await self._user_repo.create(user_data)
        except Exception as exc:
            log.error(
                "oauth_user_creation_failed",
                provider=provider,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise AppError("failed to create user") from exc

    async def _update_last_login(self, user_id: ObjectId) -> None:
        """Update the user's last_login_at timestamp. Fire-and-forget."""
        try:
            await self._user_repo.update(
                user_id,
                {"$set": {"last_login_at": datetime.now(timezone.utc)}},
            )
        except Exception as exc:
            log.error(
                "oauth_last_login_update_failed",
                user_id=str(user_id),
                error=str(exc),
                error_type=type(exc).__name__,
            )

    def _make_tokens(self, user: UserDoc, provider_key: str) -> tuple[str, str]:
        """Issue access + refresh tokens for an OAuth user."""
        return self._token_factory.issue_tokens(user, provider_key)

    # ── Public API ────────────────────────────────────────────────────────────

    async def handle_callback(
        self,
        provider_key: str,
        provider_info: ProviderInfo,
        action: str,
        state_data: dict[str, Any],
        signup_ip: str | None = None,
    ) -> AuthResult:
        """Process an OAuth callback after the route has validated state and
        fetched user info from the provider.

        Four flows (identical to the original blueprints/oauth.py):
            1. ``action == "link"``   — Link provider to an existing account.
            2. Existing OAuth user    — Direct login (provider_user_id match).
            3. Email collision        — Auto-link if provider email is verified.
            4. New user               — Create account and send welcome email.

        Args:
            provider_key:  e.g. ``"google"``, ``"github"``, ``"discord"``.
            provider_info: Normalised ProviderInfo model from the provider strategy.
            action:        ``"login"`` or ``"link"`` (from state).
            state_data:    Full state dict (may contain ``user_id`` for linking).
            signup_ip:     Client IP for new user creation.

        Returns:
            (user_doc, access_token, refresh_token)

        Raises:
            ValidationError: Invalid linking request, email mismatch.
            NotFoundError:   User not found during linking.
            ConflictError:   Provider already linked, or email collision rejected.
            AppError:        DB failure during linking or user creation.
        """
        provider_display = provider_key.title()

        # ── Account-linking flow ──────────────────────────────────────────────
        if action == OAuthAction.LINK:
            if not provider_info.email:
                raise ValidationError("email not provided by OAuth provider")
            link_user_id = state_data.get("user_id")
            if not link_user_id:
                raise ValidationError("invalid linking request")

            current_user = await self._user_repo.find_by_id(ObjectId(link_user_id))
            if not current_user:
                raise NotFoundError("user not found")

            # Ensure provider not already linked to this user
            for entry in current_user.auth_providers:
                if entry.provider == provider_key:
                    raise ConflictError(f"{provider_display} account already linked")

            # Ensure provider_user_id not already owned by another account
            existing_oauth_user = await self._user_repo.find_by_oauth_provider(
                provider_key, provider_info.provider_user_id
            )
            if existing_oauth_user and str(existing_oauth_user.id) != link_user_id:
                raise ConflictError(
                    f"This {provider_display} account is already linked to another user"
                )

            # Require email match
            if current_user.email.lower() != provider_info.email.lower():
                log.warning(
                    "oauth_email_mismatch",
                    user_id=link_user_id,
                    provider=provider_key,
                    reason="linking_attempt",
                )
                raise ValidationError(
                    "email mismatch",
                    details={
                        "message": (
                            f"The email associated with this {provider_display} account "
                            f"({provider_info.email}) does not match your account email "
                            f"({current_user.email}). "
                            f"Please use a {provider_display} account with the same email address."
                        )
                    },
                )

            success = await self._link_provider(
                ObjectId(link_user_id), provider_info, provider_key
            )
            if not success:
                log.error(
                    "oauth_linking_failed",
                    user_id=link_user_id,
                    provider=provider_key,
                )
                raise AppError(f"failed to link {provider_display} account")

            log.info(
                "oauth_account_linked", user_id=link_user_id, provider=provider_key
            )
            updated_user = await self._user_repo.find_by_id(ObjectId(link_user_id))
            access_token, refresh_token = self._make_tokens(updated_user, provider_key)
            return AuthResult(
                user=updated_user,
                access_token=access_token,
                refresh_token=refresh_token,
            )

        # ── Existing OAuth user login ─────────────────────────────────────────
        existing_oauth_user = await self._user_repo.find_by_oauth_provider(
            provider_key, provider_info.provider_user_id
        )
        if existing_oauth_user:
            await self._update_last_login(existing_oauth_user.id)
            log.info(
                "oauth_login_success",
                user_id=str(existing_oauth_user.id),
                provider=provider_key,
                action="login",
            )
            access_token, refresh_token = self._make_tokens(
                existing_oauth_user, provider_key
            )
            return AuthResult(
                user=existing_oauth_user,
                access_token=access_token,
                refresh_token=refresh_token,
            )

        # Email is required for collision detection and new user creation
        if not provider_info.email:
            raise ValidationError("email not provided by OAuth provider")

        # ── Email collision ───────────────────────────────────────────────────
        existing_email_user = await self._user_repo.find_by_email(provider_info.email)
        if existing_email_user:
            if self._can_auto_link(existing_email_user, provider_info, provider_key):
                success = await self._link_provider(
                    existing_email_user.id, provider_info, provider_key
                )
                if not success:
                    log.error(
                        "oauth_auto_link_failed",
                        user_id=str(existing_email_user.id),
                        provider=provider_key,
                    )
                    raise AppError("failed to link accounts")

                await self._update_last_login(existing_email_user.id)
                log.info(
                    "oauth_auto_linked",
                    user_id=str(existing_email_user.id),
                    provider=provider_key,
                )
                updated_user = await self._user_repo.find_by_id(existing_email_user.id)
                access_token, refresh_token = self._make_tokens(
                    updated_user, provider_key
                )
                return AuthResult(
                    user=updated_user,
                    access_token=access_token,
                    refresh_token=refresh_token,
                )
            else:
                log.warning("oauth_email_collision_rejected", provider=provider_key)
                raise ConflictError(
                    "email already exists",
                    details={
                        "message": (
                            "An account with this email already exists. "
                            "Please log in with your existing method first to link accounts."
                        )
                    },
                )

        # ── New user creation ─────────────────────────────────────────────────
        new_user_id = await self._create_oauth_user(
            provider_info, provider_key, signup_ip
        )
        log.info(
            "user_registered",
            user_id=str(new_user_id),
            auth_method=f"{provider_key}_oauth",
            email_verified=provider_info.email_verified,
        )

        # Send welcome email — non-fatal
        try:
            await self._email.send_welcome_email(
                provider_info.email, provider_info.name
            )
        except Exception as exc:
            log.error(
                "oauth_welcome_email_failed",
                provider=provider_key,
                error=str(exc),
                error_type=type(exc).__name__,
            )

        new_user = await self._user_repo.find_by_id(new_user_id)
        access_token, refresh_token = self._make_tokens(new_user, provider_key)
        return AuthResult(
            user=new_user,
            access_token=access_token,
            refresh_token=refresh_token,
            is_new=True,
        )

    async def unlink_provider(self, user_id: str, provider_name: str) -> None:
        """Unlink an OAuth provider from a user's account.

        Raises:
            NotFoundError:   User not found.
            ValidationError: Attempt to remove the last auth method when no
                                password is set.
            NotFoundError:   Provider not linked to this user.
            AppError:        DB failure.
        """
        user_oid = ObjectId(user_id)
        user = await self._user_repo.find_by_id(user_oid)
        if not user:
            raise NotFoundError("user not found")

        remaining = [p for p in user.auth_providers if p.provider != provider_name]

        if not user.password_set and len(remaining) == 0:
            raise ValidationError(
                "cannot unlink last authentication method",
                details={
                    "message": "Set a password first before unlinking your last OAuth provider"
                },
            )

        updated = await self._user_repo.update(
            user_oid,
            {
                "$pull": {"auth_providers": {"provider": provider_name}},
                "$set": {"updated_at": datetime.now(timezone.utc)},
            },
        )

        if not updated:
            log.info("oauth_unlink_not_found", user_id=user_id, provider=provider_name)
            raise NotFoundError("provider not found or already unlinked")

        log.info("oauth_provider_unlinked", user_id=user_id, provider=provider_name)

    async def list_providers(
        self, user_id: str
    ) -> tuple[list[OAuthProviderDetail], bool]:
        """Return the user's linked OAuth providers and password_set flag.

        Returns:
            (providers_list, password_set)

        Raises:
            NotFoundError: User not found.
        """
        user = await self._user_repo.find_by_id(ObjectId(user_id))
        if not user:
            raise NotFoundError("user not found")

        linked = [
            OAuthProviderDetail(
                provider=p.provider,
                email=p.email,
                email_verified=p.email_verified,
                linked_at=p.linked_at,
                profile=p.profile if p.profile else ProviderProfile(),
            )
            for p in user.auth_providers
        ]

        return linked, user.password_set
