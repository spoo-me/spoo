"""Unit tests for VerdictRepository."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from repositories.verdict_repository import VerdictRepository
from schemas.enums.safety import VerdictTier


def _col() -> AsyncMock:
    col = AsyncMock()
    col.name = "safety_verdicts"
    return col


class TestUpsertVerdict:
    @pytest.mark.asyncio
    async def test_upserts_by_host(self):
        col = _col()
        repo = VerdictRepository(col)

        await repo.upsert_verdict(
            "evil.contaboserver.net",
            registrable_domain="contaboserver.net",
            tier=VerdictTier.TOXIC,
            reason="matched blocklist pattern corr.php",
            source="local_feeds",
            trigger="report",
            sample_url="https://evil.contaboserver.net/COR/corr.php",
            context={"report_count": 3},
        )

        args, kwargs = col.update_one.await_args
        assert args[0] == {"host": "evil.contaboserver.net"}
        assert kwargs["upsert"] is True
        st = args[1]["$set"]
        assert st["tier"] == "toxic"
        assert st["registrable_domain"] == "contaboserver.net"
        assert st["decided_by"] == "system"
        # created_at only on first insert; updated_at refreshed every time.
        assert "created_at" in args[1]["$setOnInsert"]
        assert "updated_at" in st


class TestFindByHost:
    @pytest.mark.asyncio
    async def test_returns_model(self):
        col = _col()
        col.find_one = AsyncMock(
            return_value={
                "host": "evil.com",
                "registrable_domain": "evil.com",
                "tier": "toxic",
                "updated_at": datetime.now(timezone.utc),
            }
        )
        repo = VerdictRepository(col)
        doc = await repo.find_by_host("evil.com")
        assert doc is not None
        assert doc.tier == VerdictTier.TOXIC

    @pytest.mark.asyncio
    async def test_miss_returns_none(self):
        col = _col()
        col.find_one = AsyncMock(return_value=None)
        assert await VerdictRepository(col).find_by_host("clean.com") is None
