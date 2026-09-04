"""Unit tests for ApiKeyService."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from bson import ObjectId

from errors import EmailNotVerifiedError, LimitReachedError
from schemas.models.api_key import ApiKeyDoc

USER_OID = ObjectId("aaaaaaaaaaaaaaaaaaaaaaaa")
KEY_OID = ObjectId("cccccccccccccccccccccccc")


def make_repo():
    repo = AsyncMock()
    repo.count_by_user = AsyncMock(return_value=0)
    repo.insert = AsyncMock(return_value=KEY_OID)
    repo.list_by_user = AsyncMock(return_value=[])
    repo.revoke = AsyncMock(return_value=True)
    return repo


def make_service(repo=None):
    from services.api_key_service import ApiKeyService

    return ApiKeyService(repo or make_repo())


def make_key_doc(revoked=False):
    return ApiKeyDoc.from_mongo(
        {
            "_id": KEY_OID,
            "user_id": USER_OID,
            "token_prefix": "abcd1234",
            "token_hash": "x" * 64,
            "name": "My Key",
            "scopes": ["urls:read"],
            "revoked": revoked,
            "expires_at": None,
            "created_at": datetime.now(timezone.utc),
        }
    )


class TestApiKeyServiceCreate:
    @pytest.mark.asyncio
    async def test_create_returns_doc_and_prefixed_token(self):
        repo = make_repo()
        svc = make_service(repo)

        doc, raw_token = await svc.create(
            name="Test Key",
            scopes=["urls:read"],
            user_id=USER_OID,
            email_verified=True,
            max_keys=20,
        )

        assert isinstance(doc, ApiKeyDoc)
        assert raw_token.startswith("spoo_")
        assert doc.name == "Test Key"
        assert doc.scopes == ["urls:read"]
        repo.insert.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_create_raises_when_email_not_verified(self):
        svc = make_service()
        with pytest.raises(EmailNotVerifiedError):
            await svc.create(
                name="Key",
                scopes=[],
                user_id=USER_OID,
                email_verified=False,
                max_keys=20,
            )

    @pytest.mark.asyncio
    async def test_create_raises_when_at_max_limit(self):
        repo = make_repo()
        repo.count_by_user = AsyncMock(return_value=20)
        svc = make_service(repo)

        with pytest.raises(LimitReachedError):
            await svc.create(
                name="Key",
                scopes=[],
                user_id=USER_OID,
                email_verified=True,
                max_keys=20,
            )

    @pytest.mark.asyncio
    async def test_create_with_description_and_expiry(self):
        repo = make_repo()
        svc = make_service(repo)
        expires = datetime(2030, 1, 1, tzinfo=timezone.utc)

        doc, _raw_token = await svc.create(
            name="Named Key",
            scopes=["urls:manage"],
            user_id=USER_OID,
            email_verified=True,
            max_keys=20,
            description="My description",
            expires_at=expires,
        )

        assert doc.description == "My description"
        assert doc.expires_at == expires


class TestApiKeyServiceListByUser:
    @pytest.mark.asyncio
    async def test_list_by_user_delegates_to_repo(self):
        repo = make_repo()
        key = make_key_doc()
        repo.list_by_user = AsyncMock(return_value=[key])
        svc = make_service(repo)

        result = await svc.list_by_user(USER_OID)

        repo.list_by_user.assert_awaited_once_with(USER_OID)
        assert len(result) == 1
        assert result[0] is key

    @pytest.mark.asyncio
    async def test_list_by_user_empty(self):
        repo = make_repo()
        repo.list_by_user = AsyncMock(return_value=[])
        svc = make_service(repo)

        result = await svc.list_by_user(USER_OID)
        assert result == []


class TestApiKeyServiceRevoke:
    @pytest.mark.asyncio
    async def test_soft_revoke_returns_true(self):
        repo = make_repo()
        repo.revoke = AsyncMock(return_value=True)
        svc = make_service(repo)

        result = await svc.revoke(USER_OID, KEY_OID)

        repo.revoke.assert_awaited_once_with(USER_OID, KEY_OID, hard_delete=False)
        assert result is True

    @pytest.mark.asyncio
    async def test_hard_delete_returns_true(self):
        repo = make_repo()
        repo.revoke = AsyncMock(return_value=True)
        svc = make_service(repo)

        result = await svc.revoke(USER_OID, KEY_OID, hard_delete=True)

        repo.revoke.assert_awaited_once_with(USER_OID, KEY_OID, hard_delete=True)
        assert result is True

    @pytest.mark.asyncio
    async def test_revoke_not_found_returns_false(self):
        repo = make_repo()
        repo.revoke = AsyncMock(return_value=False)
        svc = make_service(repo)

        result = await svc.revoke(USER_OID, KEY_OID)

        assert result is False
