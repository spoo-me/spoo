"""TagService: the per-account registry."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.errors import ConflictError, NotFoundError, ValidationError
from app.schemas.models.tag import TAGS_MAX_PER_OWNER, TagColor, TagDoc
from app.services.tag_service import TagService

OWNER = ObjectId("c" * 24)
T1 = ObjectId("1" * 24)
T2 = ObjectId("2" * 24)


def _tag(
    tag_id: ObjectId, name: str, color: TagColor = TagColor.GRAY, icon="tag"
) -> TagDoc:
    return TagDoc(
        _id=tag_id,
        owner_id=OWNER,
        name=name,
        color=color,
        icon=icon,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _svc(existing: list[TagDoc] | None = None):
    tag_repo = AsyncMock()
    tag_repo.list_by_owner.return_value = existing or []
    tag_repo.count_by_owner.return_value = len(existing or [])
    tag_repo.insert.return_value = T1
    url_repo = AsyncMock()
    return TagService(tag_repo, url_repo), tag_repo, url_repo


class TestCreate:
    @pytest.mark.asyncio
    async def test_inserts_normalised_doc(self):
        svc, tag_repo, _ = _svc()

        doc = await svc.create(OWNER, "launch", TagColor.TEAL, "rocket")

        written = tag_repo.insert.call_args[0][0]
        assert written["name"] == "launch"
        assert written["color"] == "teal"
        assert written["icon"] == "rocket"
        assert written["owner_id"] == OWNER
        assert doc.id == T1

    @pytest.mark.asyncio
    async def test_auto_color_is_least_used_non_gray(self):
        existing = [_tag(ObjectId(), f"t{i}", TagColor.RED) for i in range(2)]
        existing.append(_tag(ObjectId(), "o", TagColor.ORANGE))
        svc, _, _ = _svc(existing)

        doc = await svc.create(OWNER, "x", None)

        assert doc.color is TagColor.AMBER

    @pytest.mark.asyncio
    async def test_duplicate_name_is_409(self):
        svc, tag_repo, _ = _svc()
        tag_repo.insert.side_effect = DuplicateKeyError("dup")

        with pytest.raises(ConflictError):
            await svc.create(OWNER, "launch", None)

    @pytest.mark.asyncio
    async def test_per_owner_cap_uses_a_count_not_a_full_load(self):
        svc, tag_repo, _ = _svc()
        tag_repo.count_by_owner.return_value = TAGS_MAX_PER_OWNER

        with pytest.raises(ValidationError, match="at most"):
            await svc.create(OWNER, "one more", None)
        tag_repo.list_by_owner.assert_not_called()


class TestUpdate:
    @pytest.mark.asyncio
    async def test_rename_recolour_and_icon(self):
        svc, tag_repo, _ = _svc()
        tag_repo.find_by_id_and_owner.return_value = _tag(
            T1, "old", TagColor.RED, "rocket"
        )

        doc = await svc.update(OWNER, T1, name="new", color=TagColor.BLUE, icon="flag")

        set_ops = tag_repo.update_by_id_and_owner.call_args[0][2]
        assert {k: set_ops[k] for k in ("name", "color", "icon")} == {
            "name": "new",
            "color": TagColor.BLUE,
            "icon": "flag",
        }
        assert set_ops["updated_at"] is not None
        assert (doc.name, doc.color, doc.icon) == ("new", TagColor.BLUE, "flag")
        assert doc.updated_at == set_ops["updated_at"]

    @pytest.mark.asyncio
    async def test_icon_untouched_unless_sent(self):
        svc, tag_repo, _ = _svc()
        tag_repo.find_by_id_and_owner.return_value = _tag(T1, "x", icon="rocket")

        await svc.update(OWNER, T1, name="y", color=None)

        assert "icon" not in tag_repo.update_by_id_and_owner.call_args[0][2]

    @pytest.mark.asyncio
    async def test_noop_skips_write(self):
        svc, tag_repo, _ = _svc()
        tag_repo.find_by_id_and_owner.return_value = _tag(T1, "same")

        await svc.update(OWNER, T1, name="same", color=None)

        tag_repo.update_by_id_and_owner.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_is_404_and_rename_clash_is_409(self):
        svc, tag_repo, _ = _svc()
        tag_repo.find_by_id_and_owner.return_value = None
        with pytest.raises(NotFoundError):
            await svc.update(OWNER, T1, name="x", color=None)

        tag_repo.find_by_id_and_owner.return_value = _tag(T1, "a")
        tag_repo.update_by_id_and_owner.side_effect = DuplicateKeyError("dup")
        with pytest.raises(ConflictError):
            await svc.update(OWNER, T1, name="b", color=None)


class TestDelete:
    @pytest.mark.asyncio
    async def test_pulls_from_links_then_deletes(self):
        svc, tag_repo, url_repo = _svc()
        tag_repo.find_by_id_and_owner.return_value = _tag(T1, "x")
        url_repo.pull_tag_id_by_owner.return_value = 7

        assert await svc.delete(OWNER, T1) == 7
        url_repo.pull_tag_id_by_owner.assert_awaited_once_with(OWNER, T1)
        tag_repo.delete_by_id_and_owner.assert_awaited_once_with(T1, OWNER)

    @pytest.mark.asyncio
    async def test_missing_is_404(self):
        svc, tag_repo, url_repo = _svc()
        tag_repo.find_by_id_and_owner.return_value = None
        with pytest.raises(NotFoundError):
            await svc.delete(OWNER, T1)
        url_repo.pull_tag_id_by_owner.assert_not_called()


class TestLookups:
    @pytest.mark.asyncio
    async def test_list_with_counts_joins_link_counts(self):
        svc, _, url_repo = _svc([_tag(T1, "a"), _tag(T2, "b")])
        url_repo.count_tag_ids_by_owner.return_value = {T1: 3}

        rows = await svc.list_with_counts(OWNER)

        assert [(d.name, n) for d, n in rows] == [("a", 3), ("b", 0)]

    @pytest.mark.asyncio
    async def test_refs_by_id_dedupes_and_keys_by_id(self):
        svc, tag_repo, _ = _svc()
        tag_repo.find_by_ids_and_owner.return_value = [_tag(T1, "a")]

        refs = await svc.refs_by_id(OWNER, [T1, T1, T2])

        tag_repo.find_by_ids_and_owner.assert_awaited_once_with([T1, T2], OWNER)
        assert list(refs) == [T1]

    @pytest.mark.asyncio
    async def test_delete_all_for_owner(self):
        svc, tag_repo, _ = _svc()
        tag_repo.delete_by_owner.return_value = 4
        assert await svc.delete_all_for_owner(OWNER) == 4


class TestAssertOwned:
    @pytest.mark.asyncio
    async def test_passes_when_all_owned_and_names_the_field_otherwise(self):
        svc, tag_repo, _ = _svc()
        tag_repo.find_by_ids_and_owner.return_value = [_tag(T1, "a")]

        await svc.assert_owned(OWNER, [T1])
        with pytest.raises(ValidationError) as exc:
            await svc.assert_owned(OWNER, [T1, T2], field="add")
        assert str(T2) in str(exc.value)
        assert exc.value.field == "add"

    @pytest.mark.asyncio
    async def test_ids_for_names(self):
        svc, tag_repo, _ = _svc()
        tag_repo.find_by_names_and_owner.return_value = [_tag(T2, "q3")]
        assert await svc.ids_for_names(OWNER, ["q3"]) == [T2]
        tag_repo.find_by_names_and_owner.assert_awaited_once_with(["q3"], OWNER)
