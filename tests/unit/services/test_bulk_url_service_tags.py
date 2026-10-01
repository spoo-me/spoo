"""BulkUrlService.bulk_tag: per-item retag by tag id with two set-based writes."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from bson import ObjectId
from pymongo.errors import PyMongoError

from app.errors import ValidationError
from app.schemas.models.tag import TagDoc

from .test_bulk_url_service import _CapturingSink, _oid, make_bulk_service
from .test_url_service import USER_OID, make_url_v2_doc

T1 = ObjectId("1" * 24)
T2 = ObjectId("2" * 24)
FULL = [ObjectId(f"{i:024x}") for i in range(100, 110)]


def _tag(tag_id: ObjectId) -> TagDoc:
    return TagDoc(
        _id=tag_id,
        owner_id=USER_OID,
        name=str(tag_id)[:4],
        created_at=datetime.now(timezone.utc),
    )


def _svc(docs, owned=None, events=None):
    url_repo = AsyncMock()
    url_repo.find_by_ids_and_owner.return_value = docs
    owned_ids = set(owned or [T1, T2])

    async def assert_owned(owner_id, ids, *, field="tag_ids"):
        missing = [str(i) for i in ids if i not in owned_ids]
        if missing:
            raise ValidationError(f"unknown tag ids: {', '.join(missing)}", field=field)

    tag_service = AsyncMock()
    tag_service.assert_owned = AsyncMock(side_effect=assert_owned)
    svc = make_bulk_service(url_repo, AsyncMock(), events=events)
    svc._tag_service = tag_service
    return svc, url_repo, tag_service


def _rows(report):
    return {row.id: row for row in report.results}


class TestBulkTag:
    @pytest.mark.asyncio
    async def test_unknown_add_id_rejects_envelope_before_load(self):
        svc, url_repo, _ = _svc([], owned=[T1])

        with pytest.raises(ValidationError, match=str(T2)):
            await svc.bulk_tag([_oid(1)], [T1, T2], [], USER_OID)
        url_repo.find_by_ids_and_owner.assert_not_called()

    @pytest.mark.asyncio
    async def test_unchanged_item_is_noop_without_write(self):
        svc, url_repo, _ = _svc([make_url_v2_doc(url_id=_oid(1), tag_ids=[T1])])

        report = await svc.bulk_tag([_oid(1)], [T1], [T2], USER_OID)

        assert report.results[0].ok is True
        url_repo.apply_by_ids_and_owner.assert_not_called()

    @pytest.mark.asyncio
    async def test_remove_and_add_is_one_pipeline_write(self):
        svc, url_repo, _ = _svc([make_url_v2_doc(url_id=_oid(1), tag_ids=[T2])])

        report = await svc.bulk_tag([_oid(1)], [T1], [T2], USER_OID)

        assert report.summary.succeeded == 1
        (call,) = url_repo.apply_by_ids_and_owner.await_args_list
        assert call.args[0] == [_oid(1)]
        assert call.args[1] == USER_OID
        (stage,) = call.args[2]
        assert "updated_at" in stage["$set"]
        expr = stage["$set"]["tag_ids"]["$let"]
        assert expr["vars"]["kept"]["$filter"]["cond"] == {
            "$not": {"$in": ["$$this", [T2]]}
        }
        assert expr["in"]["$concatArrays"][1]["$filter"]["input"] == [T1]

    @pytest.mark.asyncio
    async def test_remove_only_needs_no_ownership_check(self):
        svc, url_repo, tag_service = _svc(
            [make_url_v2_doc(url_id=_oid(1), tag_ids=[T2])]
        )

        await svc.bulk_tag([_oid(1)], [], [T2], USER_OID)

        tag_service.assert_owned.assert_not_called()
        url_repo.apply_by_ids_and_owner.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_over_cap_item_rejected_others_written(self):
        full = make_url_v2_doc(url_id=_oid(1), tag_ids=FULL)
        empty = make_url_v2_doc(url_id=_oid(2), alias="two")
        svc, url_repo, _ = _svc([full, empty])

        report = await svc.bulk_tag([_oid(1), _oid(2)], [T1], [], USER_OID)

        rows = _rows(report)
        assert rows[str(_oid(1))].error_code == "validation_error"
        assert rows[str(_oid(2))].ok is True
        (call,) = url_repo.apply_by_ids_and_owner.await_args_list
        assert call.args[0] == [_oid(2)]

    @pytest.mark.asyncio
    async def test_blocked_and_missing_verdicts(self):
        svc, url_repo, _ = _svc([make_url_v2_doc(url_id=_oid(1), status="BLOCKED")])

        report = await svc.bulk_tag([_oid(1), _oid(9)], [T1], [], USER_OID)

        rows = _rows(report)
        assert rows[str(_oid(1))].error_code == "forbidden"
        assert rows[str(_oid(9))].error_code == "not_found"
        url_repo.apply_by_ids_and_owner.assert_not_called()

    @pytest.mark.asyncio
    async def test_write_failure_marks_slice_internal(self):
        svc, url_repo, _ = _svc([make_url_v2_doc(url_id=_oid(1))])
        url_repo.apply_by_ids_and_owner.side_effect = PyMongoError("down")

        report = await svc.bulk_tag([_oid(1)], [T1], [], USER_OID)

        assert report.results[0].error_code == "internal"

    @pytest.mark.asyncio
    async def test_emits_updated_with_post_state_ids(self):
        sink = _CapturingSink()
        svc, _, _ = _svc(
            [
                make_url_v2_doc(url_id=_oid(1), tag_ids=[T2]),
                make_url_v2_doc(url_id=_oid(2), alias="two", tag_ids=[T1]),
            ],
            events=sink,
        )

        await svc.bulk_tag([_oid(1), _oid(2)], [T1], [T2], USER_OID)

        assert len(sink.events) == 1
        event = sink.events[0]
        assert event.type == "link.updated"
        assert event.data["link"]["tag_ids"] == [str(T1)]
        assert event.data["changes"]["tag_ids"] == {"old": [str(T2)], "new": [str(T1)]}
