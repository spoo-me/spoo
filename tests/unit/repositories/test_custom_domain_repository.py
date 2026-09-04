"""Unit tests for CustomDomainRepository."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from bson import ObjectId

from repositories.custom_domain_repository import CustomDomainRepository
from schemas.enums.domain_status import DomainStatus, VerificationMethod


def _doc_dict(**overrides):
    base = {
        "_id": ObjectId(),
        "fqdn": "links.example.com",
        "owner_id": ObjectId(),
        "status": DomainStatus.PENDING.value,
        "verification_method": VerificationMethod.CNAME.value,
        "verification_token": None,
        "is_system_default": False,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return base


class TestCustomDomainRepository:
    @pytest.mark.asyncio
    async def test_find_by_fqdn_returns_doc(self):
        col = AsyncMock()
        col.find_one = AsyncMock(return_value=_doc_dict())
        col.name = "custom_domains"
        repo = CustomDomainRepository(col)

        doc = await repo.find_by_fqdn("links.example.com")
        assert doc is not None
        assert doc.fqdn == "links.example.com"
        col.find_one.assert_awaited_once_with({"fqdn": "links.example.com"})

    @pytest.mark.asyncio
    async def test_find_by_fqdn_normalises_lookup_input(self):
        # A caller passing an uppercased, trailing-dot, padded variant must
        # still find the canonical row — defends against input drift between
        # the writer (DTO-validated) and ad-hoc lookup paths.
        col = AsyncMock()
        col.find_one = AsyncMock(return_value=None)
        col.name = "custom_domains"
        repo = CustomDomainRepository(col)

        await repo.find_by_fqdn("  LINKS.Example.COM.  ")
        col.find_one.assert_awaited_once_with({"fqdn": "links.example.com"})

    @pytest.mark.asyncio
    async def test_find_active_by_fqdn_normalises_lookup_input(self):
        col = AsyncMock()
        col.find_one = AsyncMock(return_value=None)
        col.name = "custom_domains"
        repo = CustomDomainRepository(col)

        await repo.find_active_by_fqdn("LINKS.Example.COM.")
        args = col.find_one.call_args.args[0]
        assert args["fqdn"] == "links.example.com"

    @pytest.mark.asyncio
    async def test_find_active_by_fqdn_scopes_to_active(self):
        col = AsyncMock()
        col.find_one = AsyncMock(return_value=None)
        col.name = "custom_domains"
        repo = CustomDomainRepository(col)

        await repo.find_active_by_fqdn("links.example.com")
        col.find_one.assert_awaited_once_with(
            {
                "fqdn": "links.example.com",
                "status": DomainStatus.ACTIVE,
                "paused_by_limit": {"$ne": True},
            }
        )

    @pytest.mark.asyncio
    async def test_find_blocking_by_fqdn_excludes_revoked(self):
        # Uniqueness gate must not consider REVOKED docs — they're terminal
        # records of past registrations and don't reserve the fqdn for
        # future use. New registrations of the same fqdn are gated by DCV,
        # not by a database cooldown.
        col = AsyncMock()
        col.find_one = AsyncMock(return_value=None)
        col.name = "custom_domains"
        repo = CustomDomainRepository(col)

        await repo.find_blocking_by_fqdn("links.example.com")
        col.find_one.assert_awaited_once_with(
            {
                "fqdn": "links.example.com",
                "status": {"$ne": DomainStatus.REVOKED},
            }
        )

    @pytest.mark.asyncio
    async def test_find_blocking_by_fqdn_normalises_lookup_input(self):
        col = AsyncMock()
        col.find_one = AsyncMock(return_value=None)
        col.name = "custom_domains"
        repo = CustomDomainRepository(col)

        await repo.find_blocking_by_fqdn("LINKS.Example.COM.")
        args = col.find_one.call_args.args[0]
        assert args["fqdn"] == "links.example.com"

    @pytest.mark.asyncio
    async def test_count_by_owner_runs_on_owner_id(self):
        col = AsyncMock()
        col.count_documents = AsyncMock(return_value=2)
        col.name = "custom_domains"
        repo = CustomDomainRepository(col)
        owner = ObjectId()

        n = await repo.count_by_owner(owner)
        assert n == 2
        col.count_documents.assert_awaited_once_with({"owner_id": owner})

    @pytest.mark.asyncio
    async def test_update_status_sets_updated_at_and_error(self):
        col = AsyncMock()
        # Mimic UpdateResult: matched_count > 0
        result = MagicMock()
        result.matched_count = 1
        col.update_one = AsyncMock(return_value=result)
        col.name = "custom_domains"
        repo = CustomDomainRepository(col)
        domain_id = ObjectId()

        ok = await repo.update_status(
            domain_id, DomainStatus.SUSPENDED, last_verification_error="DNS NXDOMAIN"
        )
        assert ok is True
        args, _kwargs = col.update_one.call_args
        query, ops = args
        assert query == {"_id": domain_id}
        assert ops["$set"]["status"] == DomainStatus.SUSPENDED
        assert ops["$set"]["last_verification_error"] == "DNS NXDOMAIN"
        assert "updated_at" in ops["$set"]
        # bump_last_verified_at default False — no key written
        assert "last_verified_at" not in ops["$set"]

    @pytest.mark.asyncio
    async def test_update_status_bumps_last_verified_when_requested(self):
        col = AsyncMock()
        result = MagicMock()
        result.matched_count = 1
        col.update_one = AsyncMock(return_value=result)
        col.name = "custom_domains"
        repo = CustomDomainRepository(col)

        await repo.update_status(
            ObjectId(),
            DomainStatus.ACTIVE,
            bump_last_verified_at=True,
        )
        ops = col.update_one.call_args.args[1]
        assert "last_verified_at" in ops["$set"]

    @pytest.mark.asyncio
    async def test_set_eviction_pending_records_state_and_error(self):
        col = AsyncMock()
        result = MagicMock()
        result.matched_count = 1
        col.update_one = AsyncMock(return_value=result)
        col.name = "custom_domains"
        repo = CustomDomainRepository(col)
        domain_id = ObjectId()

        ok = await repo.set_eviction_pending(domain_id, pending=True, error="caddy 500")
        assert ok is True
        ops = col.update_one.call_args.args[1]
        assert ops["$set"]["eviction_pending"] is True
        assert ops["$set"]["last_eviction_error"] == "caddy 500"
        assert "updated_at" in ops["$set"]

    @pytest.mark.asyncio
    async def test_set_eviction_pending_clears_error_on_success(self):
        col = AsyncMock()
        result = MagicMock()
        result.matched_count = 1
        col.update_one = AsyncMock(return_value=result)
        col.name = "custom_domains"
        repo = CustomDomainRepository(col)

        await repo.set_eviction_pending(ObjectId(), pending=False)
        ops = col.update_one.call_args.args[1]
        assert ops["$set"]["eviction_pending"] is False
        assert ops["$set"]["last_eviction_error"] is None

    @pytest.mark.asyncio
    async def test_list_by_owner_paginates_and_orders_by_created_at_desc(self):
        # Underpins the dashboard listing endpoint — must filter by owner,
        # sort newest first, and respect skip/limit so pagination works.
        col = AsyncMock()
        cursor = MagicMock()
        cursor.sort = MagicMock(return_value=cursor)
        cursor.skip = MagicMock(return_value=cursor)
        cursor.limit = MagicMock(return_value=cursor)
        cursor.to_list = AsyncMock(return_value=[])
        col.find = MagicMock(return_value=cursor)
        col.name = "custom_domains"
        repo = CustomDomainRepository(col)
        owner_id = ObjectId()

        await repo.list_by_owner(owner_id, skip=20, limit=10)

        # (1) filters by owner_id
        assert col.find.call_args.args[0] == {"owner_id": owner_id}
        # (2) sorts by created_at descending
        cursor.sort.assert_called_once_with("created_at", -1)
        # (3) respects skip + limit
        cursor.skip.assert_called_once_with(20)
        cursor.limit.assert_called_once_with(10)
        cursor.to_list.assert_awaited_once_with(length=10)

    @pytest.mark.asyncio
    async def test_find_stale_active_excludes_system_default(self):
        col = AsyncMock()
        cursor = MagicMock()
        cursor.sort = MagicMock(return_value=cursor)
        cursor.limit = MagicMock(return_value=cursor)
        cursor.to_list = AsyncMock(return_value=[])
        col.find = MagicMock(return_value=cursor)
        col.name = "custom_domains"
        repo = CustomDomainRepository(col)

        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        await repo.find_stale_active(cutoff, limit=10)

        query = col.find.call_args.args[0]
        assert query["status"] == DomainStatus.ACTIVE
        assert query["is_system_default"] == {"$ne": True}
