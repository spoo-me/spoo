"""Tags on the wire: the /tags routes, tag refs embedded on links, bulk by id."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

from bson import ObjectId
from fastapi.testclient import TestClient

from app.dependencies import (
    get_bulk_url_service,
    get_current_user,
    get_tag_service,
    get_url_service,
    require_auth,
)
from app.errors import ConflictError, NotFoundError
from app.schemas.dto.responses.bulk import (
    BulkOperationSummary,
    BulkUrlOperationResponse,
    BulkUrlResultRow,
)
from app.schemas.models.tag import TagDoc

from .conftest import _build_test_app, _make_api_key_doc, _make_url_doc, _make_user

T1 = ObjectId("1" * 24)
T2 = ObjectId("2" * 24)
VALID_ID = "665f0c2f9e7a4b1d2c3d4e5f"


def _tag(
    tag_id: ObjectId, owner: ObjectId, name: str, color="violet", icon="tag"
) -> TagDoc:
    return TagDoc(
        _id=tag_id,
        owner_id=owner,
        name=name,
        color=color,
        icon=icon,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _client(user, *, url_svc=None, tag_svc=None, bulk_svc=None):
    overrides = {
        require_auth: lambda: user,
        get_current_user: lambda: user,
        get_url_service: lambda: url_svc or AsyncMock(),
        get_tag_service: lambda: tag_svc or AsyncMock(),
    }
    if bulk_svc is not None:
        overrides[get_bulk_url_service] = lambda: bulk_svc
    return TestClient(_build_test_app(overrides), raise_server_exceptions=True)


class TestTagRoutes:
    def test_list_returns_items_with_counts(self):
        user = _make_user()
        tag_svc = AsyncMock()
        tag_svc.list_with_counts = AsyncMock(
            return_value=[(_tag(T1, user.user_id, "launch", icon="rocket"), 3)]
        )

        with _client(user, tag_svc=tag_svc) as client:
            resp = client.get("/api/v1/tags")

        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert (
            item["id"],
            item["name"],
            item["color"],
            item["icon"],
            item["link_count"],
        ) == (
            str(T1),
            "launch",
            "violet",
            "rocket",
            3,
        )

    def test_create_normalises_and_returns_201(self):
        user = _make_user()
        tag_svc = AsyncMock()
        tag_svc.create = AsyncMock(
            return_value=_tag(T1, user.user_id, "launch", "teal", "flag")
        )

        with _client(user, tag_svc=tag_svc) as client:
            resp = client.post(
                "/api/v1/tags",
                json={"name": " Launch ", "color": "teal", "icon": "flag"},
            )

        assert resp.status_code == 201
        assert resp.json()["name"] == "launch"
        tag_svc.create.assert_awaited_once_with(user.user_id, "launch", "teal", "flag")

    def test_create_bad_icon_is_422_and_conflict_is_409(self):
        user = _make_user()
        tag_svc = AsyncMock()
        tag_svc.create = AsyncMock(side_effect=ConflictError("dup"))

        with _client(user, tag_svc=tag_svc) as client:
            assert (
                client.post(
                    "/api/v1/tags", json={"name": "x", "icon": "unicorn"}
                ).status_code
                == 422
            )
            assert client.post("/api/v1/tags", json={"name": "x"}).status_code == 409

    def test_update_passes_fields_and_rejects_null_icon(self):
        user = _make_user()
        tag_svc = AsyncMock()
        tag_svc.update = AsyncMock(return_value=_tag(T1, user.user_id, "renamed"))
        tag_svc.link_count = AsyncMock(return_value=2)

        with _client(user, tag_svc=tag_svc) as client:
            resp = client.patch(
                f"/api/v1/tags/{T1}", json={"name": "Renamed", "icon": "flag"}
            )
            nulled = client.patch(f"/api/v1/tags/{T1}", json={"icon": None})

        assert resp.status_code == 200
        assert resp.json()["link_count"] == 2
        tag_svc.update.assert_awaited_once_with(
            user.user_id, T1, name="renamed", color=None, icon="flag"
        )
        assert nulled.status_code == 422

    def test_update_bad_id_is_400_and_missing_is_404(self):
        user = _make_user()
        tag_svc = AsyncMock()
        tag_svc.update = AsyncMock(side_effect=NotFoundError("Tag not found"))

        with _client(user, tag_svc=tag_svc) as client:
            assert (
                client.patch("/api/v1/tags/nope", json={"name": "x"}).status_code == 400
            )
            assert (
                client.patch(f"/api/v1/tags/{T1}", json={"name": "x"}).status_code
                == 404
            )

    def test_delete_reports_links_updated(self):
        user = _make_user()
        tag_svc = AsyncMock()
        tag_svc.delete = AsyncMock(return_value=5)

        with _client(user, tag_svc=tag_svc) as client:
            resp = client.delete(f"/api/v1/tags/{T1}")

        assert resp.status_code == 200
        assert resp.json() == {"deleted": True, "links_updated": 5}

    def test_requires_auth_and_manage_scope(self):
        anon = _build_test_app(
            {get_current_user: lambda: None, get_tag_service: lambda: AsyncMock()}
        )
        with TestClient(anon, raise_server_exceptions=False) as client:
            assert client.get("/api/v1/tags").status_code == 401

        reader = _make_user(api_key_doc=_make_api_key_doc(scopes=["urls:read"]))
        tag_svc = AsyncMock()
        tag_svc.list_with_counts = AsyncMock(return_value=[])
        with _client(reader, tag_svc=tag_svc) as client:
            assert client.get("/api/v1/tags").status_code == 200
            assert client.post("/api/v1/tags", json={"name": "x"}).status_code == 403


class TestTagsOnLinkRoutes:
    def test_shorten_passes_ids_and_embeds_refs(self):
        user = _make_user()
        url_doc = _make_url_doc(owner_id=user.user_id)
        url_doc.tag_ids = [T1]
        url_svc = AsyncMock()
        url_svc.create = AsyncMock(return_value=(url_doc, None))
        tag_svc = AsyncMock()
        tag_svc.refs_by_id = AsyncMock(
            return_value={T1: _tag(T1, user.user_id, "launch")}
        )

        with _client(user, url_svc=url_svc, tag_svc=tag_svc) as client:
            resp = client.post(
                "/api/v1/shorten",
                json={"long_url": "https://example.com", "tag_ids": [str(T1)]},
            )

        assert resp.status_code == 201
        assert resp.json()["tags"] == [
            {"id": str(T1), "name": "launch", "color": "violet", "icon": "tag"}
        ]
        assert url_svc.create.call_args[0][0].tag_ids == [str(T1)]

    def test_shorten_bad_tag_id_is_422(self):
        user = _make_user()
        with _client(user) as client:
            resp = client.post(
                "/api/v1/shorten",
                json={"long_url": "https://example.com", "tag_ids": ["launch"]},
            )
        assert resp.status_code == 422

    def test_patch_and_get_embed_refs(self):
        user = _make_user()
        url_doc = _make_url_doc(owner_id=user.user_id)
        url_doc.updated_at = datetime(2024, 6, 1, tzinfo=timezone.utc)
        url_doc.tag_ids = [T2, T1]
        url_svc = AsyncMock()
        url_svc.update = AsyncMock(return_value=url_doc)
        url_svc.get_owned = AsyncMock(return_value=url_doc)
        tag_svc = AsyncMock()
        tag_svc.refs_by_id = AsyncMock(
            return_value={
                T1: _tag(T1, user.user_id, "launch"),
                T2: _tag(T2, user.user_id, "q3", "teal"),
            }
        )

        with _client(user, url_svc=url_svc, tag_svc=tag_svc) as client:
            patched = client.patch(
                f"/api/v1/urls/{ObjectId()}", json={"tag_ids": [str(T2), str(T1)]}
            )
            got = client.get(f"/api/v1/urls/{ObjectId()}")

        assert [t["name"] for t in patched.json()["tags"]] == ["q3", "launch"]
        assert [t["name"] for t in got.json()["tags"]] == ["q3", "launch"]
        assert url_svc.update.call_args[0][1].tag_ids == [str(T2), str(T1)]

    def test_list_embeds_refs_with_one_lookup(self):
        user = _make_user()
        a = _make_url_doc(alias="a", owner_id=user.user_id)
        a.tag_ids = [T1]
        b = _make_url_doc(alias="b", owner_id=user.user_id)
        b.tag_ids = [T1, T2]
        url_svc = AsyncMock()
        url_svc.list_by_owner = AsyncMock(
            return_value={
                "items": [a, b],
                "page": 1,
                "pageSize": 20,
                "total": 2,
                "hasNext": False,
                "sortBy": "created_at",
                "sortOrder": "descending",
            }
        )
        tag_svc = AsyncMock()
        tag_svc.refs_by_id = AsyncMock(
            return_value={T1: _tag(T1, user.user_id, "launch")}
        )

        with _client(user, url_svc=url_svc, tag_svc=tag_svc) as client:
            resp = client.get(
                "/api/v1/urls",
                params={
                    "filter": '{"tagIds": ["' + str(T1) + '"], "tagsMatch": "all"}'
                },
            )

        assert resp.status_code == 200
        tag_svc.refs_by_id.assert_awaited_once_with(user.user_id, [T1, T1, T2])
        assert [[t["name"] for t in i["tags"]] for i in resp.json()["items"]] == [
            ["launch"],
            ["launch"],
        ]
        query = url_svc.list_by_owner.call_args[0][1]
        assert query.parsed_filter.tag_ids == [str(T1)]
        assert query.parsed_filter.tags_match == "all"


class TestBulkTagRoute:
    def test_passes_object_ids_and_owner(self):
        user = _make_user()
        bulk_svc = AsyncMock()
        bulk_svc.bulk_tag = AsyncMock(
            return_value=BulkUrlOperationResponse(
                summary=BulkOperationSummary(total=1, succeeded=1, failed=0),
                results=[BulkUrlResultRow(id=VALID_ID, alias="promo", ok=True)],
            )
        )

        with _client(user, bulk_svc=bulk_svc) as client:
            resp = client.post(
                "/api/v1/urls/bulk/tags",
                json={"ids": [VALID_ID], "add": [str(T1)], "remove": [str(T2)]},
            )

        assert resp.status_code == 200
        bulk_svc.bulk_tag.assert_awaited_once_with(
            [ObjectId(VALID_ID)], [T1], [T2], user.user_id
        )

    def test_envelope_rejections(self):
        user = _make_user()
        with _client(user, bulk_svc=AsyncMock()) as client:
            assert (
                client.post(
                    "/api/v1/urls/bulk/tags", json={"ids": [VALID_ID]}
                ).status_code
                == 422
            )
            assert (
                client.post(
                    "/api/v1/urls/bulk/tags",
                    json={"ids": [VALID_ID], "add": ["launch"]},
                ).status_code
                == 422
            )
