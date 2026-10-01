"""Tags across the tag model, the URL model and every DTO that carries them."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from bson import ObjectId
from pydantic import ValidationError

from app.schemas.dto.requests.bulk import BulkTagUrlsRequest
from app.schemas.dto.requests.stats import LinkStatsQuery, StatsQuery
from app.schemas.dto.requests.tag import CreateTagRequest, UpdateTagRequest
from app.schemas.dto.requests.url import (
    CreateUrlRequest,
    ListUrlsQuery,
    UpdateUrlRequest,
)
from app.schemas.dto.responses.tag import TagRef, TagResponse
from app.schemas.dto.responses.url import UpdateUrlResponse, UrlListItem, UrlResponse
from app.schemas.models.tag import TagColor, TagDoc
from app.schemas.models.url import UrlV2Doc
from app.shared.tags import TAGS_MAX_PER_LINK

T1 = ObjectId("a" * 24)
T2 = ObjectId("b" * 24)
OWNER = ObjectId("c" * 24)


def _doc(**overrides) -> UrlV2Doc:
    base = {
        "_id": ObjectId(),
        "alias": "promo",
        "owner_id": OWNER,
        "domain": "spoo.me",
        "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2024, 1, 2, tzinfo=timezone.utc),
        "long_url": "https://example.com",
        "status": "ACTIVE",
    }
    base.update(overrides)
    return UrlV2Doc.from_mongo(base)


def _tag(
    tag_id: ObjectId, name: str, color: str = "violet", icon: str | None = None
) -> TagDoc:
    return TagDoc(
        _id=tag_id,
        owner_id=OWNER,
        name=name,
        color=color,
        icon=icon,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


class TestTagDoc:
    def test_name_is_normalised(self):
        assert _tag(T1, " Launch ").name == "launch"

    def test_bad_name_rejected(self):
        with pytest.raises(ValidationError):
            _tag(T1, "a,b")

    def test_icon_must_be_curated_and_defaults_to_tag(self):
        assert _tag(T1, "x", icon="rocket").icon == "rocket"
        assert _tag(T1, "x", icon="").icon == "tag"
        assert _tag(T1, "x").icon == "tag"
        with pytest.raises(ValidationError):
            _tag(T1, "x", icon="not-a-real-icon")

    def test_color_defaults_gray(self):
        assert (
            TagDoc(
                owner_id=OWNER, name="x", created_at=datetime.now(timezone.utc)
            ).color
            is TagColor.GRAY
        )


class TestUrlV2DocTagIds:
    def test_missing_field_reads_as_empty_list(self):
        assert _doc().tag_ids == []

    def test_ids_round_trip(self):
        assert _doc(tag_ids=[T1, T2]).tag_ids == [T1, T2]


class TestCreateUrlRequestTagIds:
    def test_default_is_none(self):
        assert (
            CreateUrlRequest.model_validate({"long_url": "https://example.com"}).tag_ids
            is None
        )

    def test_dedupes_and_keeps_order(self):
        req = CreateUrlRequest.model_validate(
            {"long_url": "https://example.com", "tag_ids": [str(T2), str(T1), str(T2)]}
        )
        assert req.tag_ids == [str(T2), str(T1)]

    def test_bad_id_is_422(self):
        with pytest.raises(ValidationError):
            CreateUrlRequest.model_validate(
                {"long_url": "https://example.com", "tag_ids": ["launch"]}
            )

    def test_over_cap_rejected(self):
        ids = [f"{i:024x}" for i in range(11)]
        with pytest.raises(ValidationError):
            CreateUrlRequest.model_validate(
                {"long_url": "https://example.com", "tag_ids": ids}
            )


class TestUpdateUrlRequestTagIds:
    def test_null_reads_as_clear_and_is_in_fields_set(self):
        req = UpdateUrlRequest.model_validate({"tag_ids": None})
        assert req.tag_ids is None
        assert "tag_ids" in req.model_fields_set

    def test_omitted_is_not_in_fields_set(self):
        assert "tag_ids" not in UpdateUrlRequest.model_validate({}).model_fields_set


class TestListUrlsQueryTagFilter:
    def test_ids_names_and_match_parsed(self):
        q = ListUrlsQuery.model_validate(
            {
                "filter": json.dumps(
                    {"tagIds": [str(T1)], "tagNames": ["Launch"], "tagsMatch": "all"}
                )
            }
        )
        assert q.parsed_filter.tag_ids == [str(T1)]
        assert q.parsed_filter.tag_names == ["launch"]
        assert q.parsed_filter.tags_match == "all"

    def test_match_defaults_to_any(self):
        q = ListUrlsQuery.model_validate({"filter": json.dumps({"tagIds": [str(T1)]})})
        assert q.parsed_filter.tags_match == "any"

    def test_bad_id_rejected(self):
        with pytest.raises(ValidationError):
            ListUrlsQuery.model_validate({"filter": json.dumps({"tagIds": ["nope"]})})

    def test_empty_lists_read_as_no_filter(self):
        q = ListUrlsQuery.model_validate(
            {"filter": json.dumps({"tagIds": [], "tagNames": []})}
        )
        assert q.parsed_filter.tag_ids is None
        assert q.parsed_filter.tag_names is None

    def test_name_filter_is_not_capped_like_an_assignment(self):
        names = [f"t{i}" for i in range(TAGS_MAX_PER_LINK + 1)]
        q = ListUrlsQuery.model_validate(
            {"filter": json.dumps({"tagNames": names, "tagsMatch": "any"})}
        )
        assert q.parsed_filter.tag_names == names


class TestResponsesEmbedTags:
    def test_list_item_embeds_refs_in_link_order(self):
        refs = {T1: _tag(T1, "launch", icon="rocket"), T2: _tag(T2, "q3", "teal")}
        item = UrlListItem.from_doc(_doc(tag_ids=[T2, T1]), refs)
        assert [t.model_dump() for t in item.tags] == [
            {"id": str(T2), "name": "q3", "color": "teal", "icon": "tag"},
            {"id": str(T1), "name": "launch", "color": "violet", "icon": "rocket"},
        ]

    def test_unknown_ids_are_skipped(self):
        item = UrlListItem.from_doc(_doc(tag_ids=[T1, T2]), {T1: _tag(T1, "launch")})
        assert [t.id for t in item.tags] == [str(T1)]

    def test_without_refs_is_empty(self):
        assert UrlListItem.from_doc(_doc(tag_ids=[T1])).tags == []

    def test_create_and_update_responses(self):
        refs = {T1: _tag(T1, "launch")}
        assert (
            UrlResponse.from_doc(_doc(tag_ids=[T1]), "https://spoo.me", tag_refs=refs)
            .tags[0]
            .name
            == "launch"
        )
        assert (
            UpdateUrlResponse.from_doc(_doc(tag_ids=[T1]), refs).tags[0].name
            == "launch"
        )

    def test_tag_response_shape(self):
        resp = TagResponse.from_doc(_tag(T1, "launch", icon="flag"), 3)
        assert resp.model_dump()["link_count"] == 3
        assert resp.model_dump()["icon"] == "flag"
        assert TagRef.from_doc(_tag(T1, "launch")).model_dump() == {
            "id": str(T1),
            "name": "launch",
            "color": "violet",
            "icon": "tag",
        }


class TestTagRequests:
    def test_create_normalises_name(self):
        req = CreateTagRequest(name=" Launch ", color="teal", icon="rocket")
        assert (req.name, req.color, req.icon) == ("launch", TagColor.TEAL, "rocket")

    def test_create_color_optional_and_icon_defaults(self):
        req = CreateTagRequest(name="x")
        assert req.color is None and req.icon == "tag"

    def test_create_rejects_bad_name_color_icon(self):
        with pytest.raises(ValidationError):
            CreateTagRequest(name="a,b")
        with pytest.raises(ValidationError):
            CreateTagRequest(name="x", color="magenta")
        with pytest.raises(ValidationError):
            CreateTagRequest(name="x", icon="unicorn")

    def test_update_icon_optional_but_never_null(self):
        assert UpdateTagRequest.model_validate({}).icon is None
        assert UpdateTagRequest.model_validate({"icon": "flag"}).icon == "flag"
        with pytest.raises(ValidationError):
            UpdateTagRequest.model_validate({"icon": None})


class TestBulkTagUrlsRequest:
    def test_ids_deduped_and_converted(self):
        req = BulkTagUrlsRequest(
            ids=[str(T1)], add=[str(T2), str(T2)], remove=[str(T1)]
        )
        assert req.add_ids() == [T2]
        assert req.remove_ids() == [T1]

    def test_neither_rejected(self):
        with pytest.raises(ValidationError, match="at least one tag"):
            BulkTagUrlsRequest(ids=[str(T1)])

    def test_same_id_in_both_rejected(self):
        with pytest.raises(ValidationError, match="both added and removed"):
            BulkTagUrlsRequest(ids=[str(T1)], add=[str(T2)], remove=[str(T2)])

    def test_bad_id_rejected(self):
        with pytest.raises(ValidationError):
            BulkTagUrlsRequest(ids=[str(T1)], add=["launch"])


class TestStatsQueryTagFilters:
    def test_tag_names_normalised(self):
        assert StatsQuery(tag="Launch, q3").parsed_filters["tag"] == ["launch", "q3"]

    def test_tag_ids_validated(self):
        assert StatsQuery(tag_id=str(T1)).parsed_filters["tag_id"] == [str(T1)]
        with pytest.raises(ValidationError):
            StatsQuery(tag_id="nope")

    def test_both_accepted_in_filters_json(self):
        q = StatsQuery(filters=json.dumps({"tag": ["Launch"], "tag_id": [str(T1)]}))
        assert q.parsed_filters["tag"] == ["launch"]
        assert q.parsed_filters["tag_id"] == [str(T1)]

    @pytest.mark.parametrize("dim", ["tag", "tag_id"])
    def test_never_a_group_by_dimension(self, dim):
        with pytest.raises(ValidationError):
            StatsQuery(group_by=dim)

    def test_link_stats_query_drops_both(self):
        q = LinkStatsQuery(filters=json.dumps({"tag": ["launch"], "tag_id": [str(T1)]}))
        assert "tag" not in q.parsed_filters and "tag_id" not in q.parsed_filters
