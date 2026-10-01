"""Unit tests for ensure_system_default_domain.

Verifies the idempotent upsert: fresh DB seeds a new row, re-run on a populated
DB returns the existing _id without rewriting ``created_at``.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from bson import ObjectId

from app.infrastructure.bootstrap.system_default_domain import (
    ensure_system_default_domain,
)
from app.schemas.models.base import ANONYMOUS_OWNER_ID

DOMAIN_OID = ObjectId("eeeeeeeeeeeeeeeeeeeeeeee")


def _db_with_collection(col: AsyncMock) -> MagicMock:
    db = MagicMock()
    db.__getitem__ = lambda self, name: col
    return db


class TestEnsureSystemDefaultDomain:
    @pytest.mark.asyncio
    async def test_seeds_new_row_when_missing(self):
        col = AsyncMock()
        col.update_one = AsyncMock(return_value=MagicMock(upserted_id=DOMAIN_OID))

        result = await ensure_system_default_domain(_db_with_collection(col), "spoo.me")

        assert result == DOMAIN_OID
        # Inspect the upsert call shape
        call = col.update_one.call_args
        filter_doc, update_doc = call.args[0], call.args[1]
        assert filter_doc == {"fqdn": "spoo.me"}
        assert update_doc["$setOnInsert"]["fqdn"] == "spoo.me"
        assert update_doc["$setOnInsert"]["status"] == "active"
        assert update_doc["$setOnInsert"]["verification_method"] == "system"
        assert update_doc["$setOnInsert"]["is_system_default"] is True
        assert update_doc["$setOnInsert"]["owner_id"] == ANONYMOUS_OWNER_ID
        assert "updated_at" in update_doc["$set"]
        # created_at must be in $setOnInsert only — having it in $set too
        # would overwrite the original timestamp on every boot.
        assert "created_at" not in update_doc["$set"]

    @pytest.mark.asyncio
    async def test_returns_existing_id_when_already_seeded(self):
        col = AsyncMock()
        col.update_one = AsyncMock(return_value=MagicMock(upserted_id=None))
        col.find_one = AsyncMock(return_value={"_id": DOMAIN_OID})

        result = await ensure_system_default_domain(_db_with_collection(col), "spoo.me")

        assert result == DOMAIN_OID
        col.find_one.assert_awaited_once_with({"fqdn": "spoo.me"}, {"_id": 1})

    @pytest.mark.asyncio
    async def test_idempotent_across_calls(self):
        # Two consecutive calls must both succeed and return the same _id.
        col = AsyncMock()
        col.update_one = AsyncMock(
            side_effect=[
                MagicMock(upserted_id=DOMAIN_OID),  # first call seeds
                MagicMock(upserted_id=None),  # second call no-ops
            ]
        )
        col.find_one = AsyncMock(return_value={"_id": DOMAIN_OID})

        first = await ensure_system_default_domain(_db_with_collection(col), "spoo.me")
        second = await ensure_system_default_domain(_db_with_collection(col), "spoo.me")

        assert first == second == DOMAIN_OID

    @pytest.mark.asyncio
    async def test_self_hoster_uses_their_domain(self):
        # Self-hoster sets APP_URL=https://my.shortener.dev → fqdn passed in
        # ends up as the seeded row's fqdn. No spoo.me hardcoding.
        col = AsyncMock()
        col.update_one = AsyncMock(return_value=MagicMock(upserted_id=DOMAIN_OID))

        await ensure_system_default_domain(_db_with_collection(col), "my.shortener.dev")

        update_doc = col.update_one.call_args.args[1]
        assert update_doc["$setOnInsert"]["fqdn"] == "my.shortener.dev"

    @pytest.mark.asyncio
    async def test_raises_when_row_vanishes_after_upsert(self):
        # update_one says no insert (existing row) but find_one returns None —
        # only possible if something deleted the row mid-call. Defensive raise.
        col = AsyncMock()
        col.update_one = AsyncMock(return_value=MagicMock(upserted_id=None))
        col.find_one = AsyncMock(return_value=None)

        with pytest.raises(RuntimeError, match="vanished"):
            await ensure_system_default_domain(_db_with_collection(col), "spoo.me")
