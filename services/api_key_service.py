"""
ApiKeyService — API key lifecycle management.

Handles creation, listing, and revocation of API keys.
Framework-agnostic: no FastAPI imports.
"""

from __future__ import annotations

from datetime import datetime, timezone

from bson import ObjectId

from errors import EmailNotVerifiedError, LimitReachedError
from infrastructure.crypto import hash_token
from infrastructure.logging import get_logger
from repositories.api_key_repository import ApiKeyRepository
from schemas.models.api_key import ApiKeyDoc
from services.features.catalog import UNLIMITED
from shared.generators import generate_secure_token

log = get_logger(__name__)


class ApiKeyService:
    def __init__(
        self,
        api_key_repo: ApiKeyRepository,
    ) -> None:
        self._repo = api_key_repo

    async def create(
        self,
        name: str,
        scopes: list[str],
        user_id: ObjectId,
        email_verified: bool,
        description: str | None = None,
        expires_at: datetime | None = None,
        *,
        max_keys: int,
    ) -> tuple[ApiKeyDoc, str]:
        """Create a new API key.

        Returns:
            (ApiKeyDoc, raw_token) — the raw token (prefixed with ``spoo_``) is
            returned ONLY here and is hashed before storage.

        Raises:
            EmailNotVerifiedError: User has not verified their email.
            ValidationError: User already has MAX_ACTIVE_KEYS active keys.
        """
        if not email_verified:
            log.info(
                "api_key_create_rejected",
                reason="email_not_verified",
                user_id=str(user_id),
            )
            raise EmailNotVerifiedError("Email verification required")

        if max_keys != UNLIMITED:
            active_count = await self._repo.count_by_user(user_id)
            if active_count >= max_keys:
                log.info(
                    "api_key_create_rejected",
                    reason="max_limit_reached",
                    user_id=str(user_id),
                    count=active_count,
                )
                raise LimitReachedError(
                    "api_keys_max", max_=max_keys, current=active_count
                )

        raw = generate_secure_token()
        token_prefix = raw[:8]
        token_hash = hash_token(raw)
        now = datetime.now(timezone.utc)

        api_key_doc = ApiKeyDoc(
            user_id=user_id,
            token_prefix=token_prefix,
            token_hash=token_hash,
            name=name,
            description=description,
            scopes=scopes,
            expires_at=expires_at,
            created_at=now,
            revoked=False,
        )
        doc_data = api_key_doc.model_dump(by_alias=True, exclude={"id"})

        key_id = await self._repo.insert(doc_data)
        api_key_doc.id = key_id

        log.info(
            "api_key_created",
            user_id=str(user_id),
            key_id=str(key_id),
            key_prefix=token_prefix,
            scopes=scopes,
            expires_at=expires_at.isoformat() if expires_at else None,
        )

        return api_key_doc, f"spoo_{raw}"

    async def list_by_user(self, user_id: ObjectId) -> list[ApiKeyDoc]:
        """Return all API keys for a user (active and revoked)."""
        return await self._repo.list_by_user(user_id)

    async def revoke(
        self,
        user_id: ObjectId,
        key_id: ObjectId,
        *,
        hard_delete: bool = False,
    ) -> bool:
        """Soft-revoke or hard-delete an API key.

        Returns:
            True if the operation affected a document (ownership confirmed).
        """
        ok = await self._repo.revoke(user_id, key_id, hard_delete=hard_delete)
        if ok:
            action = "deleted" if hard_delete else "revoked"
            log.info(
                f"api_key_{action}",
                user_id=str(user_id),
                key_id=str(key_id),
            )
        else:
            log.info(
                "api_key_action_failed",
                user_id=str(user_id),
                key_id=str(key_id),
                action="delete" if hard_delete else "revoke",
                reason="not_found_or_access_denied",
            )
        return ok
