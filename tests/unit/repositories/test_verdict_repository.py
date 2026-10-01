"""Unit tests for VerdictRepository."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.repositories.verdict_repository import VerdictRepository
from app.schemas.enums.safety import VerdictTier


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
        assert args[0] == {
            "host": "evil.contaboserver.net",
            "decided_by": {"$in": ["system", None]},
        }
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


class TestHumanVerdictGuard:
    @pytest.mark.asyncio
    async def test_system_writes_only_match_system_docs(self):
        col = _col()
        repo = VerdictRepository(col)
        written = await repo.upsert_verdict(
            "x.example",
            registrable_domain="example",
            tier=VerdictTier.UNCERTAIN,
            reason=None,
            source="screening",
            trigger="sweep",
        )
        assert written is True
        query = col.update_one.await_args.args[0]
        assert query == {"host": "x.example", "decided_by": {"$in": ["system", None]}}

    @pytest.mark.asyncio
    async def test_a_human_verdict_rejects_the_system_upsert(self):
        from pymongo.errors import DuplicateKeyError

        col = _col()
        col.update_one = AsyncMock(side_effect=DuplicateKeyError("E11000 host"))
        repo = VerdictRepository(col)
        written = await repo.upsert_verdict(
            "x.example",
            registrable_domain="example",
            tier=VerdictTier.TOXIC,
            reason="model said scam",
            source="llm",
            trigger="report",
        )
        assert written is False

    @pytest.mark.asyncio
    async def test_a_human_write_matches_by_host_alone(self):
        col = _col()
        repo = VerdictRepository(col)
        await repo.upsert_verdict(
            "x.example",
            registrable_domain="example",
            tier=VerdictTier.BENIGN,
            reason="checked by hand",
            source="human",
            trigger="operator",
            decided_by="human",
        )
        assert col.update_one.await_args.args[0] == {"host": "x.example"}
