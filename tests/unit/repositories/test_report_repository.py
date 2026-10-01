"""Unit tests for the report repositories' account-erasure surface."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pymongo.errors import PyMongoError

from app.repositories.report_repository import (
    ReportRepository,
    ReportSubmissionRepository,
)

from .conftest import USER_OID, make_collection


class TestPullReporter:
    @pytest.mark.asyncio
    async def test_pulls_reporter_id_from_matching_reports(self):
        col = make_collection()
        col.update_many = AsyncMock(return_value=MagicMock(modified_count=3))
        count = await ReportRepository(col).pull_reporter(USER_OID)
        col.update_many.assert_awaited_once_with(
            {"reporter_ids": USER_OID},
            {"$pull": {"reporter_ids": USER_OID}},
        )
        assert count == 3

    @pytest.mark.asyncio
    async def test_propagates_pymongo_error(self):
        col = make_collection()
        col.update_many = AsyncMock(side_effect=PyMongoError("conn lost"))
        with pytest.raises(PyMongoError):
            await ReportRepository(col).pull_reporter(USER_OID)


class TestDeleteByReporter:
    @pytest.mark.asyncio
    async def test_deletes_by_id_or_followup_email(self):
        col = make_collection()
        col.delete_many = AsyncMock(return_value=MagicMock(deleted_count=2))
        count = await ReportSubmissionRepository(col).delete_by_reporter(
            USER_OID, "user@example.com"
        )
        col.delete_many.assert_awaited_once_with(
            {
                "$or": [
                    {"reporter_id": USER_OID},
                    {"reporter_email": "user@example.com"},
                ]
            }
        )
        assert count == 2
