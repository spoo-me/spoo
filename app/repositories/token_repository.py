"""
Repository for the `verification-tokens` MongoDB collection.

Tokens are used for email verification, password resets, OTP flows, and device auth codes.
All methods are async. Returns VerificationTokenDoc models where applicable.
Errors are handled by BaseRepository for standard operations; domain-specific
methods (consume_by_hash, find_latest_by_user) handle their own errors.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bson import ObjectId
from pymongo.errors import PyMongoError

from app.infrastructure.logging import get_logger
from app.repositories.base import BaseRepository
from app.schemas.models.token import VerificationTokenDoc

log = get_logger(__name__)


class TokenRepository(BaseRepository[VerificationTokenDoc]):
    async def create(self, token_data: dict) -> ObjectId:
        """Insert a new verification token document. Returns the inserted _id."""
        return await self._insert(token_data)

    async def find_by_hash_and_type(
        self, token_hash: str, token_type: str
    ) -> VerificationTokenDoc | None:
        """Find a non-used token by its SHA-256 hash and type."""
        return await self._find_one(
            {
                "token_hash": token_hash,
                "token_type": token_type,
                "used_at": None,
            }
        )

    async def find_valid_by_hash(
        self, token_hash: str, token_type: str
    ) -> VerificationTokenDoc | None:
        """Read an unused, non-expired token WITHOUT consuming it.

        Same liveness filter as ``consume_by_hash``, no write — the
        look-before-flip half of the deletion-restore path, where burning
        the single-use token before the guarded status flip would make a
        transient flip failure terminal for OAuth-only accounts.
        """
        now = datetime.now(timezone.utc)
        return await self._find_one(
            {
                "token_hash": token_hash,
                "token_type": token_type,
                "used_at": None,
                "expires_at": {"$gt": now},
            }
        )

    async def consume_by_hash(
        self, token_hash: str, token_type: str
    ) -> VerificationTokenDoc | None:
        """Atomically find an unused, non-expired token and mark it as used.

        Returns the pre-update document, or None if no matching token exists.
        """
        now = datetime.now(timezone.utc)
        try:
            doc = await self._col.find_one_and_update(
                {
                    "token_hash": token_hash,
                    "token_type": token_type,
                    "used_at": None,
                    "expires_at": {"$gt": now},
                },
                {"$set": {"used_at": now}},
            )
            return VerificationTokenDoc.from_mongo(doc)
        except PyMongoError as exc:
            log.error(
                "repo_consume_by_hash_failed",
                collection=self._collection_name,
                token_type=token_type,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    async def mark_as_used(self, token_id: ObjectId) -> bool:
        """Mark a token as consumed by setting ``used_at`` to now.

        Returns True if a document was modified.
        """
        return await self._update(
            {"_id": token_id},
            {"$set": {"used_at": datetime.now(timezone.utc)}},
        )

    async def consume_if_unused(self, token_id: ObjectId) -> bool:
        """Atomically consume a token only if it is still unused and not expired.

        Returns True if a document was modified (i.e. the token was valid and
        successfully consumed).  Returns False if the token was already used,
        expired, or not found — preventing double-use in concurrent requests.
        """
        now = datetime.now(timezone.utc)
        return await self._update(
            {"_id": token_id, "used_at": None, "expires_at": {"$gt": now}},
            {"$set": {"used_at": now}},
        )

    async def find_latest_by_user(
        self, user_id: ObjectId, token_type: str
    ) -> VerificationTokenDoc | None:
        """Find the most recent non-used token for a user and type."""
        try:
            doc = await self._col.find_one(
                {
                    "user_id": user_id,
                    "token_type": token_type,
                    "used_at": None,
                },
                sort=[("created_at", -1)],
            )
            return VerificationTokenDoc.from_mongo(doc)
        except PyMongoError as exc:
            log.error(
                "repo_find_latest_by_user_failed",
                collection=self._collection_name,
                user_id=str(user_id),
                token_type=token_type,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    async def increment_attempts(self, token_id: ObjectId) -> bool:
        """Atomically increment the ``attempts`` counter on a token."""
        return await self._update(
            {"_id": token_id},
            {"$inc": {"attempts": 1}},
        )

    async def delete_by_user(
        self,
        user_id: ObjectId,
        token_type: str | None = None,
        app_id: str | None = None,
    ) -> int:
        """Delete all tokens for a user, optionally filtered by token type and app_id.

        Returns the number of documents deleted.
        """
        query: dict = {"user_id": user_id}
        if token_type is not None:
            query["token_type"] = token_type
        if app_id is not None:
            query["app_id"] = app_id
        return await self._delete_many(query)

    async def delete_by_hash(self, token_hash: str, token_type: str) -> int:
        """Delete the token with this exact hash — precision cleanup for a
        mint whose follow-up write failed. Never touches sibling tokens, so
        a concurrent request's freshly-minted token always survives.
        Returns the number of documents deleted (0 or 1).
        """
        return await self._delete_many(
            {"token_hash": token_hash, "token_type": token_type}
        )

    async def delete_by_user_or_email(self, user_id: ObjectId, email: str) -> int:
        """Delete every token tied to the user id OR the email address.

        Account-erasure path: pre-signup flows key tokens by email alone,
        so an id-only sweep would leave the address behind. A falsy email
        drops that clause — ``{"email": ""}`` would match OTHER accounts'
        tokens stored with an empty address; the user_id clause always
        stays. Returns the number of documents deleted.
        """
        if not email:
            return await self._delete_many({"user_id": user_id})
        return await self._delete_many(
            {"$or": [{"user_id": user_id}, {"email": email}]}
        )

    async def count_recent(
        self, user_id: ObjectId, token_type: str, minutes: int = 60
    ) -> int:
        """Count tokens created within the last *minutes* minutes.

        Used for rate-limiting token issuance.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        return await self._count(
            {
                "user_id": user_id,
                "token_type": token_type,
                "created_at": {"$gte": cutoff},
            }
        )
