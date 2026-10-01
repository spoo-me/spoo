"""UrlService: tag ids on create, patch and the list filter."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from bson import ObjectId

from app.errors import ValidationError
from app.schemas.dto.requests.url import (
    CreateUrlRequest,
    ListUrlsQuery,
    UpdateUrlRequest,
)
from app.schemas.models.tag import TagDoc

from .test_url_service import (
    URL_OID,
    USER_OID,
    make_repos,
    make_service,
    make_url_v2_doc,
)

T1 = ObjectId("a" * 24)
T2 = ObjectId("b" * 24)


def _tag(tag_id: ObjectId, name: str) -> TagDoc:
    return TagDoc(
        _id=tag_id, owner_id=USER_OID, name=name, created_at=datetime.now(timezone.utc)
    )


def _svc(owned: list[TagDoc] | None = None):
    from app.services.url_service import UrlService

    url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache = make_repos()
    blocked_url_repo.get_patterns.return_value = []
    owned_ids = {t.id for t in (owned or [])}

    async def assert_owned(owner_id, ids, *, field="tag_ids"):
        missing = [str(i) for i in ids if i not in owned_ids]
        if missing:
            raise ValidationError(f"unknown tag ids: {', '.join(missing)}", field=field)

    tag_service = AsyncMock()
    tag_service.assert_owned = AsyncMock(side_effect=assert_owned)
    tag_service.ids_for_names.return_value = [t.id for t in (owned or [])]
    base = make_service(url_repo, legacy_repo, emoji_repo, blocked_url_repo, url_cache)
    svc = UrlService.__new__(UrlService)
    svc.__dict__.update(base.__dict__)
    svc._tag_service = tag_service
    return svc, url_repo, tag_service


class TestCreateTagIds:
    @pytest.mark.asyncio
    async def test_create_persists_owned_tag_ids(self):
        svc, url_repo, tag_service = _svc([_tag(T1, "launch"), _tag(T2, "q3")])
        url_repo.check_alias_exists.return_value = False
        url_repo.insert.return_value = ObjectId()

        req = CreateUrlRequest(
            long_url="https://example.com", tag_ids=[str(T1), str(T2)]
        )
        await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")

        assert url_repo.insert.call_args[0][0]["tag_ids"] == [T1, T2]
        tag_service.assert_owned.assert_awaited_once_with(USER_OID, [T1, T2])

    @pytest.mark.asyncio
    async def test_create_rejects_foreign_tag_id(self):
        svc, url_repo, _ = _svc([_tag(T1, "launch")])
        url_repo.check_alias_exists.return_value = False

        req = CreateUrlRequest(
            long_url="https://example.com", tag_ids=[str(T1), str(T2)]
        )
        with pytest.raises(ValidationError, match=str(T2)):
            await svc.create(req, owner_id=USER_OID, client_ip="1.2.3.4")
        url_repo.insert.assert_not_called()

    @pytest.mark.asyncio
    async def test_anonymous_create_with_tags_rejected(self):
        svc, url_repo, _ = _svc()
        url_repo.check_alias_exists.return_value = False

        req = CreateUrlRequest(long_url="https://example.com", tag_ids=[str(T1)])
        with pytest.raises(ValidationError, match="account"):
            await svc.create(req, owner_id=None, client_ip="1.2.3.4")

    @pytest.mark.asyncio
    async def test_create_without_tags_writes_empty_list(self):
        svc, url_repo, tag_service = _svc()
        url_repo.check_alias_exists.return_value = False
        url_repo.insert.return_value = ObjectId()

        await svc.create(
            CreateUrlRequest(long_url="https://example.com"),
            owner_id=USER_OID,
            client_ip="1.2.3.4",
        )

        assert url_repo.insert.call_args[0][0]["tag_ids"] == []
        tag_service.assert_owned.assert_not_called()


class TestUpdateTagIds:
    @pytest.mark.asyncio
    async def test_update_replaces_whole_list(self):
        svc, url_repo, _ = _svc([_tag(T2, "q3")])
        url_repo.find_by_id.return_value = make_url_v2_doc(tag_ids=[T1])
        url_repo.update.return_value = True

        await svc.update(URL_OID, UpdateUrlRequest(tag_ids=[str(T2)]), USER_OID)

        assert url_repo.update.call_args[0][1]["$set"]["tag_ids"] == [T2]

    @pytest.mark.asyncio
    async def test_update_null_clears(self):
        svc, url_repo, _ = _svc()
        url_repo.find_by_id.return_value = make_url_v2_doc(tag_ids=[T1])
        url_repo.update.return_value = True

        await svc.update(URL_OID, UpdateUrlRequest(tag_ids=None), USER_OID)

        assert url_repo.update.call_args[0][1]["$set"]["tag_ids"] == []

    @pytest.mark.asyncio
    async def test_update_identical_is_noop(self):
        svc, url_repo, _ = _svc([_tag(T1, "launch")])
        url_repo.find_by_id.return_value = make_url_v2_doc(tag_ids=[T1])

        await svc.update(URL_OID, UpdateUrlRequest(tag_ids=[str(T1)]), USER_OID)

        url_repo.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_foreign_id_rejected(self):
        svc, url_repo, _ = _svc([])
        url_repo.find_by_id.return_value = make_url_v2_doc()

        with pytest.raises(ValidationError):
            await svc.update(URL_OID, UpdateUrlRequest(tag_ids=[str(T2)]), USER_OID)


class TestListTagFilter:
    @pytest.mark.asyncio
    async def test_ids_any_builds_in(self):
        svc, url_repo, _ = _svc()
        url_repo.count_by_query.return_value = 0
        url_repo.find_by_owner.return_value = []

        q = ListUrlsQuery(filter=f'{{"tagIds": ["{T1}", "{T2}"]}}')
        await svc.list_by_owner(USER_OID, q)

        assert url_repo.count_by_query.call_args[0][0]["tag_ids"] == {"$in": [T1, T2]}

    @pytest.mark.asyncio
    async def test_all_builds_all(self):
        svc, url_repo, _ = _svc()
        url_repo.count_by_query.return_value = 0
        url_repo.find_by_owner.return_value = []

        q = ListUrlsQuery(filter=f'{{"tagIds": ["{T1}"], "tagsMatch": "all"}}')
        await svc.list_by_owner(USER_OID, q)

        assert url_repo.count_by_query.call_args[0][0]["tag_ids"] == {"$all": [T1]}

    @pytest.mark.asyncio
    async def test_names_resolve_through_registry(self):
        svc, url_repo, tag_service = _svc([_tag(T2, "q3")])
        url_repo.count_by_query.return_value = 0
        url_repo.find_by_owner.return_value = []

        q = ListUrlsQuery(filter=f'{{"tagIds": ["{T1}"], "tagNames": ["Q3"]}}')
        await svc.list_by_owner(USER_OID, q)

        tag_service.ids_for_names.assert_awaited_once_with(USER_OID, ["q3"])
        assert url_repo.count_by_query.call_args[0][0]["tag_ids"] == {"$in": [T1, T2]}

    @pytest.mark.asyncio
    async def test_unknown_names_match_nothing(self):
        svc, url_repo, _ = _svc([])
        url_repo.count_by_query.return_value = 0
        url_repo.find_by_owner.return_value = []

        await svc.list_by_owner(
            USER_OID, ListUrlsQuery(filter='{"tagNames": ["ghost"]}')
        )

        assert url_repo.count_by_query.call_args[0][0]["tag_ids"] == {"$in": []}

    @pytest.mark.asyncio
    async def test_all_with_an_unknown_name_matches_nothing(self):
        svc, url_repo, _ = _svc([_tag(T2, "q3")])
        url_repo.count_by_query.return_value = 0
        url_repo.find_by_owner.return_value = []

        q = ListUrlsQuery(filter='{"tagNames": ["q3", "ghost"], "tagsMatch": "all"}')
        await svc.list_by_owner(USER_OID, q)

        assert url_repo.count_by_query.call_args[0][0]["tag_ids"] == {"$all": []}
