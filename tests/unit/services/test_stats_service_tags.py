"""StatsService: tag scopes resolve through the registry and the link collection."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from bson import ObjectId

from schemas.models.tag import TagDoc

from .test_stats_service import OWNER_ID, _q, facet_response, make_service

A = ObjectId("a" * 24)
B = ObjectId("b" * 24)
T1 = ObjectId("1" * 24)
T2 = ObjectId("2" * 24)
NOTHING = ObjectId("0" * 24)


def _match(click_repo):
    return click_repo.aggregate.call_args[0][0][0]["$match"]


def _tag(tag_id: ObjectId, name: str) -> TagDoc:
    return TagDoc(
        _id=tag_id,
        owner_id=ObjectId(OWNER_ID),
        name=name,
        created_at=datetime.now(timezone.utc),
    )


def _svc(named: list[TagDoc] | None = None):
    svc, click_repo, url_repo = make_service()
    click_repo.aggregate.return_value = facet_response()
    tag_service = AsyncMock()
    tag_service.ids_for_names.return_value = [t.id for t in (named or [])]
    svc._tag_service = tag_service
    return svc, click_repo, url_repo, tag_service


class TestTagScope:
    @pytest.mark.asyncio
    async def test_tag_id_becomes_url_id_arm(self):
        svc, click_repo, url_repo, _ = _svc()
        url_repo.list_ids_by_owner_and_tag_ids.return_value = [A, B]

        await svc.query(query=_q(tag_id=str(T1)), owner_id=OWNER_ID, window_days=90)

        url_repo.list_ids_by_owner_and_tag_ids.assert_awaited_once_with(
            ObjectId(OWNER_ID), [T1]
        )
        match = _match(click_repo)
        assert match["meta.url_id"] == {"$in": [A, B]}
        assert match["meta.owner_id"] == ObjectId(OWNER_ID)
        assert "tag_id" not in match and "tag" not in match

    @pytest.mark.asyncio
    async def test_tag_names_resolve_through_registry(self):
        svc, _, url_repo, tag_service = _svc([_tag(T2, "launch")])
        url_repo.list_ids_by_owner_and_tag_ids.return_value = [A]

        await svc.query(
            query=_q(tag="Launch", tag_id=str(T1)), owner_id=OWNER_ID, window_days=90
        )

        tag_service.ids_for_names.assert_awaited_once_with(
            ObjectId(OWNER_ID), ["launch"]
        )
        url_repo.list_ids_by_owner_and_tag_ids.assert_awaited_once_with(
            ObjectId(OWNER_ID), [T1, T2]
        )

    @pytest.mark.asyncio
    async def test_unknown_name_matches_nothing(self):
        svc, click_repo, url_repo, _ = _svc([])

        await svc.query(query=_q(tag="ghost"), owner_id=OWNER_ID, window_days=90)

        url_repo.list_ids_by_owner_and_tag_ids.assert_not_called()
        assert _match(click_repo)["meta.url_id"] == {"$in": [NOTHING]}

    @pytest.mark.asyncio
    async def test_unused_tag_matches_nothing(self):
        svc, click_repo, url_repo, _ = _svc()
        url_repo.list_ids_by_owner_and_tag_ids.return_value = []

        await svc.query(query=_q(tag_id=str(T1)), owner_id=OWNER_ID, window_days=90)

        assert _match(click_repo)["meta.url_id"] == {"$in": [NOTHING]}

    @pytest.mark.asyncio
    async def test_tag_intersects_explicit_url_id(self):
        svc, click_repo, url_repo, _ = _svc()
        url_repo.list_ids_by_owner_and_tag_ids.return_value = [A, B]

        await svc.query(
            query=_q(tag_id=str(T1), url_id=str(B)), owner_id=OWNER_ID, window_days=90
        )

        assert _match(click_repo)["meta.url_id"] == {"$in": [B]}

    @pytest.mark.asyncio
    async def test_response_echoes_the_tag_filters_not_the_ids(self):
        svc, _, url_repo, _ = _svc()
        url_repo.list_ids_by_owner_and_tag_ids.return_value = [A]

        resp = await svc.query(
            query=_q(tag_id=str(T1)), owner_id=OWNER_ID, window_days=90
        )

        assert resp["filters"] == {"tag_id": [str(T1)]}

    @pytest.mark.asyncio
    async def test_no_tag_filter_skips_resolution(self):
        svc, _, url_repo, tag_service = _svc()

        await svc.query(query=_q(), owner_id=OWNER_ID, window_days=90)

        url_repo.list_ids_by_owner_and_tag_ids.assert_not_called()
        tag_service.ids_for_names.assert_not_called()
