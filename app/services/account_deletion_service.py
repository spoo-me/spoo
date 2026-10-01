"""Account deletion requests + grace-period restore (GDPR Art. 17 intake).

The interactive half of account deletion: ``request_deletion`` flips an
ACTIVE account to PENDING_DELETION after re-authentication and mails a
one-shot restore link, ``restore`` cancels a pending deletion with
credentials, ``restore_with_token`` cancels it with the emailed link —
the only cancel path for OAuth-only accounts, which have no password to
restore with. The destructive half — the cascade that actually erases
data — lives in ``AccountErasureService``, driven by the scheduled sweep
once ``purge_after`` passes.

Token revocation note: session and device refresh tokens are stateless
JWTs with no server-side store, so there is nothing to delete here.
Every refresh path (``CredentialService.refresh_token``, the device
flows) re-fetches the user and requires ACTIVE status, so the flip to
PENDING_DELETION kills rotation immediately; outstanding ACCESS tokens
live to their short expiry — same caveat as any status change.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from app.errors import AppError, ConflictError, ForbiddenError, NotFoundError
from app.infrastructure.crypto import hash_password, hash_token, verify_password
from app.infrastructure.logging import get_logger
from app.schemas.models.token import TOKEN_TYPE_DELETION_RESTORE
from app.schemas.models.user import UserDoc, UserStatus
from app.services.account_erasure_service import ErasureMailer, NoopErasureMailer
from app.shared.generators import generate_secure_token

if TYPE_CHECKING:
    from bson import ObjectId

    from app.repositories.token_repository import TokenRepository
    from app.repositories.user_repository import UserRepository

log = get_logger(__name__)

# One message for every re-auth failure — which credential was wrong (or
# missing) must not leak to a stolen-session attacker probing the account.
_REAUTH_FAILED = "re-authentication failed"

# One message for every restore failure — wrong password, unknown email,
# bad token, and not-pending all answer identically (no account enumeration).
_RESTORE_FAILED = "unable to restore account"

# Burned on restore's early-failure paths so response timing doesn't
# reveal account existence or type (CWE-203); computed once at import.
_TIMING_DUMMY_HASH = hash_password("spoo-restore-timing-equalizer")


class AccountDeletionService:
    """Deletion intake and grace-period restore for one account.

    Args:
        user_repo:  Repository for the ``users`` collection.
        token_repo: Repository for ``verification-tokens`` — stores the
                    hash of the one-shot restore token (None disables the
                    emailed cancel link, unit-test convenience only).
        mailer:     Deletion lifecycle mail (Noop when ZeptoMail is
                    unconfigured).
        grace_days: Days between the request and the erasure sweep
                    picking the account up (``settings.account_deletion_grace_days``).
    """

    def __init__(
        self,
        user_repo: UserRepository,
        *,
        token_repo: TokenRepository | None = None,
        mailer: ErasureMailer | None = None,
        grace_days: int = 7,
    ) -> None:
        self._user_repo = user_repo
        self._token_repo = token_repo
        self._mailer = mailer or NoopErasureMailer()
        self._grace_days = grace_days

    async def request_deletion(
        self,
        user_id: ObjectId,
        *,
        password: str | None,
        confirm_email: str | None,
    ) -> datetime:
        """Flip the account to PENDING_DELETION. Returns the purge deadline.

        Raises:
            NotFoundError:  The authenticated user no longer exists.
            ConflictError:  Deletion already requested (409).
            ForbiddenError: Re-authentication failed (403) — uniform
                message regardless of which credential was wrong.
        """
        svc_log = log.bind(op="account.request_deletion")

        user = await self._user_repo.find_by_id(user_id)
        if user is None:
            raise NotFoundError("user not found")
        if user.status in (UserStatus.PENDING_DELETION, UserStatus.ERASING):
            raise ConflictError("account deletion already requested")

        self._verify_reauth(user, password, confirm_email)

        # Password accounts mint best-effort AFTER the flip (credentials can
        # always cancel); OAuth-only BEFORE — see _request_oauth_only_deletion.
        if user.password_hash:
            purge_after = await self._flip_to_pending(user.id)
            restore_token = await self._mint_restore_token(user, purge_after)
        else:
            purge_after, restore_token = await self._request_oauth_only_deletion(user)

        svc_log.info(
            "account_deletion_requested",
            user_id=str(user.id),
            grace_days=self._grace_days,
        )
        await self._notify_requested(user.email, purge_after, restore_token)
        return purge_after

    async def restore(self, email: str, password: str) -> None:
        """Cancel a pending deletion with email + password credentials.

        OAuth-only accounts (no password) cannot restore here — they use
        the one-shot link from the deletion notice (``restore_with_token``).

        Raises:
            ForbiddenError: Uniform 403 for wrong credentials, unknown
                email, and accounts not pending deletion — the endpoint
                must not become an account-state oracle.
        """
        svc_log = log.bind(op="account.restore")

        user = await self._user_repo.find_by_email(email.strip().lower())
        if user is None or not user.password_hash:
            # Burn a verify anyway so timing can't distinguish "no such
            # account / OAuth-only" from "wrong password" (CWE-203).
            verify_password(password, _TIMING_DUMMY_HASH)
            svc_log.info("account_restore_failed", reason="invalid_credentials")
            raise ForbiddenError(_RESTORE_FAILED)
        if not verify_password(password, user.password_hash):
            svc_log.info(
                "account_restore_failed",
                reason="invalid_credentials",
                user_id=str(user.id),
            )
            raise ForbiddenError(_RESTORE_FAILED)
        if not await self._user_repo.restore(user.id):
            # Not PENDING_DELETION (restored/purged first, or ERASING —
            # the cascade claimed it and erasure is final).
            svc_log.info(
                "account_restore_failed",
                reason="not_pending",
                user_id=str(user.id),
            )
            raise ForbiddenError(_RESTORE_FAILED)

        await self._discard_restore_tokens(user.id)
        svc_log.info("account_restored", user_id=str(user.id))
        await self._notify_cancelled(user.email)

    async def restore_with_token(self, restore_token: str) -> None:
        """Cancel a pending deletion with the one-shot emailed token.

        Order is load-bearing: validate the token WITHOUT consuming it,
        attempt the guarded restore flip, consume only on success —
        consuming first would let a transient flip error burn the only
        cancel proof an OAuth-only account has. A raced double-click still
        restores once: both racers validate, the PENDING_DELETION-only
        flip picks one winner, the loser answers the uniform 403, and the
        winner consumes. The consume itself is best-effort — replay of an
        unburned token hits the not-pending filter and 403s.

        Raises:
            ForbiddenError: Uniform 403 for unknown, expired, consumed
                tokens and accounts not pending deletion — indistinguishable
                from the credential path's failures.
        """
        svc_log = log.bind(op="account.restore")

        if self._token_repo is None:
            svc_log.info("account_restore_failed", reason="token_repo_unconfigured")
            raise ForbiddenError(_RESTORE_FAILED)

        token_hash = hash_token(restore_token)
        token_doc = await self._token_repo.find_valid_by_hash(
            token_hash, TOKEN_TYPE_DELETION_RESTORE
        )
        if token_doc is None:
            svc_log.info("account_restore_failed", reason="invalid_token")
            raise ForbiddenError(_RESTORE_FAILED)
        if not await self._user_repo.restore(token_doc.user_id):
            svc_log.info(
                "account_restore_failed",
                reason="not_pending",
                user_id=str(token_doc.user_id),
            )
            raise ForbiddenError(_RESTORE_FAILED)

        await self._consume_restore_token(token_hash)
        svc_log.info("account_restored", user_id=str(token_doc.user_id))
        await self._notify_cancelled(token_doc.email)

    # ── Internal ────────────────────────────────────────────────────────

    async def _flip_to_pending(self, user_id: ObjectId) -> datetime:
        """Guarded flip to PENDING_DELETION; returns the stored deadline.

        Raises:
            ConflictError: Lost a race with a concurrent request (the
                guarded transition matches ACTIVE only) — same answer as
                the pre-check.
        """
        flipped = await self._user_repo.mark_pending_deletion(user_id, self._grace_days)
        if not flipped:
            raise ConflictError("account deletion already requested")

        # Read back the stored deadline — the repo computed it, and the
        # response must echo exactly what the sweep will honour.
        updated = await self._user_repo.find_by_id(user_id)
        if updated is None or updated.purge_after is None:
            # The flip succeeded, so this only happens on a pathological
            # race with the sweep; recompute the same deadline.
            return datetime.now(timezone.utc) + timedelta(days=self._grace_days)
        return updated.purge_after

    async def _request_oauth_only_deletion(self, user: UserDoc) -> tuple[datetime, str]:
        """Token-first deletion for an OAuth-only account.

        Computes the purge deadline up front and threads the same ``now``
        into the flip, so the persisted token's expiry equals the stored
        ``purge_after`` exactly. Mint failure aborts with a 500 while the
        account is still ACTIVE; a flip failure best-effort deletes the
        just-minted (never emailed) token before propagating. Invariant:
        an OAuth-only account is never PENDING_DELETION without a
        persisted restore token — the emailed link is its ONLY cancel path
        (credential restore impossible, a second deletion request 409s),
        so a proof-less pending deletion would erase at ``purge_after``
        with no way to stop it. Password accounts deliberately differ:
        credentials always restore, so their mint stays best-effort after
        the flip and a lost link only degrades the notice mail.
        """
        now = datetime.now(timezone.utc)
        purge_after = now + timedelta(days=self._grace_days)
        restore_token, token_hash = await self._mint_restore_token_or_raise(
            user, purge_after
        )
        try:
            flipped = await self._user_repo.mark_pending_deletion(
                user.id, self._grace_days, now=now
            )
        except Exception:
            await self._discard_token_by_hash(token_hash)
            raise
        if not flipped:
            # Lost a race with a concurrent request — clean up only OUR
            # token (by hash): the winning request's proof must survive.
            await self._discard_token_by_hash(token_hash)
            raise ConflictError("account deletion already requested")
        return purge_after, restore_token

    @staticmethod
    def _verify_reauth(
        user: UserDoc, password: str | None, confirm_email: str | None
    ) -> None:
        """Prove the request comes from the account owner, not a session.

        Password accounts re-auth with the password ONLY — accepting the
        typed-email fallback there would let a hijacked session skip the
        password. OAuth-only accounts (no flag, no hash) type their exact
        account email. An inconsistent doc — ``password_set`` without a
        hash, or a hash without the flag — fails closed on the password.
        """
        if user.password_hash:
            if password and verify_password(password, user.password_hash):
                return
        elif not user.password_set:
            supplied = (confirm_email or "").encode()
            if secrets.compare_digest(supplied, user.email.encode()):
                return
        log.info(
            "account_deletion_reauth_failed",
            op="account.request_deletion",
            user_id=str(user.id),
            has_password=user.password_set,
        )
        raise ForbiddenError(_REAUTH_FAILED)

    async def _mint_restore_token(
        self, user: UserDoc, purge_after: datetime
    ) -> str | None:
        """Mint the one-shot restore token; only its hash is stored.

        Password-account path (post-flip). Expiry rides ``purge_after`` —
        the link dies exactly when the grace period does (and the TTL index
        reaps the doc). Best-effort: a minting failure must not fail the
        deletion request the user was just re-authenticated for; the notice
        then omits the link and credentials remain the restore path.
        OAuth-only accounts use ``_mint_restore_token_or_raise`` instead.
        """
        if self._token_repo is None:
            return None
        try:
            # A re-request after restore supersedes any stale token.
            await self._token_repo.delete_by_user(user.id, TOKEN_TYPE_DELETION_RESTORE)
            restore_token = generate_secure_token(32)
            await self._token_repo.create(
                {
                    "user_id": user.id,
                    "email": user.email,
                    "token_hash": hash_token(restore_token),
                    "token_type": TOKEN_TYPE_DELETION_RESTORE,
                    "expires_at": purge_after,
                    "created_at": datetime.now(timezone.utc),
                    "used_at": None,
                    "attempts": 0,
                }
            )
            return restore_token
        except Exception as exc:
            log.error(
                "account_deletion_restore_token_failed",
                user_id=str(user.id),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return None

    async def _mint_restore_token_or_raise(
        self, user: UserDoc, purge_after: datetime
    ) -> tuple[str, str]:
        """Mint the restore token for an OAuth-only deletion — MUST succeed.

        Unlike the password path's best-effort mint, a failure here raises
        (500) so the account stays ACTIVE. No stale-token supersede either:
        deleting the user's other tokens could destroy the proof a
        concurrently-won request just minted, and unused extras die with
        the TTL index at their ``expires_at`` anyway. Returns
        ``(token, token_hash)`` so a failed flip can clean up precisely.

        Raises:
            AppError: token repo unconfigured or persistence failed.
        """
        if self._token_repo is None:
            raise AppError("unable to prepare the account restore link")
        restore_token = generate_secure_token(32)
        token_hash = hash_token(restore_token)
        try:
            await self._token_repo.create(
                {
                    "user_id": user.id,
                    "email": user.email,
                    "token_hash": token_hash,
                    "token_type": TOKEN_TYPE_DELETION_RESTORE,
                    "expires_at": purge_after,
                    "created_at": datetime.now(timezone.utc),
                    "used_at": None,
                    "attempts": 0,
                }
            )
        except Exception as exc:
            log.error(
                "account_deletion_restore_token_failed",
                user_id=str(user.id),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise AppError("unable to prepare the account restore link") from exc
        return restore_token, token_hash

    async def _discard_token_by_hash(self, token_hash: str) -> None:
        """Best-effort cleanup of OUR just-minted token after a failed
        flip — by hash, never by user, so a concurrent winner's token
        survives. The account is not pending, so a leaked token is inert
        (restore refuses non-pending accounts) and the TTL index reaps it.
        """
        if self._token_repo is None:
            return
        try:
            await self._token_repo.delete_by_hash(
                token_hash, TOKEN_TYPE_DELETION_RESTORE
            )
        except Exception as exc:
            log.warning("account_deletion_token_cleanup_failed", error=str(exc))

    async def _consume_restore_token(self, token_hash: str) -> None:
        """Best-effort burn AFTER a successful restore flip. A failed
        consume is harmless: the account is no longer PENDING_DELETION, so
        a replay of the unburned token 403s on the guarded flip.
        """
        try:
            await self._token_repo.consume_by_hash(
                token_hash, TOKEN_TYPE_DELETION_RESTORE
            )
        except Exception as exc:
            log.warning("account_restore_token_consume_failed", error=str(exc))

    async def _discard_restore_tokens(self, user_id: ObjectId) -> None:
        """Best-effort: a credential restore invalidates the emailed link."""
        if self._token_repo is None:
            return
        try:
            await self._token_repo.delete_by_user(user_id, TOKEN_TYPE_DELETION_RESTORE)
        except Exception as exc:
            log.warning("account_restore_token_cleanup_failed", error=str(exc))

    async def _notify_requested(
        self, email: str, purge_after: datetime, restore_token: str | None
    ) -> None:
        """Best-effort grace-period notice — log-and-continue on failure.

        The deletion request must not fail on a mail outage; the user
        already sees the deadline in the response. Never log the address.
        """
        try:
            await self._mailer.send_deletion_requested(
                email, purge_after, restore_token
            )
        except Exception as exc:
            log.warning("account_deletion_mail_failed", error=str(exc))

    async def _notify_cancelled(self, email: str) -> None:
        """Best-effort cancellation notice — log-and-continue on failure.

        A silent restore would hide an attacker cancelling the deletion a
        victim asked for, so the account address always hears about it.
        Never log the address.
        """
        try:
            await self._mailer.send_deletion_cancelled(email)
        except Exception as exc:
            log.warning("account_restore_mail_failed", error=str(exc))
