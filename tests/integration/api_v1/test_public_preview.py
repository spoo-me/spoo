"""Tests for GET /api/v1/public/preview/{short_code}.

The wire contract is frozen against the spoo-landing preview page mock —
status-agnostic resolution, destination-only-while-active withholding,
geo grouping. All repos are dict-backed mocks; the real
PublicPreviewService runs against them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from dependencies.services import get_public_preview_service
from schemas.models.url import UrlV2Doc
from services.entitlements import for_plan
from services.features.catalog import Plan
from services.public_link_resolver import PublicLinkResolver
from services.public_preview_service import PublicPreviewService

from .conftest import _build_test_app, _make_url_doc

# ── Fixtures / helpers ─────────────────────────────────────────────────────────


def _make_v2(alias: str = "testme", **overrides) -> UrlV2Doc:
    """A validated UrlV2Doc variant of the shared conftest doc."""
    return UrlV2Doc(**{**_make_url_doc(alias).to_mongo(), **overrides})


def _make_v1(**overrides) -> dict:
    """A raw v1 document dict, as the legacy repos return them."""
    doc = {
        "_id": "abc123",
        "url": "https://example.com/page",
        "password": None,
        "total-clicks": 42,
        "max-clicks": None,
        "expiration-time": None,
        "creation-date": "2024-03-10",
        "creation-time": "12:00:00",
    }
    doc.update(overrides)
    return doc


def _make_service(
    v2_docs: tuple[UrlV2Doc, ...] = (),
    v1_docs: dict[str, dict] | None = None,
    emoji_docs: dict[str, dict] | None = None,
    plan: Plan = Plan.FREE,
    flag_on: bool = False,
) -> PublicPreviewService:
    """Real service over dict-backed repo mocks (exact-match lookups)."""
    v2_by_key = {(d.alias, d.domain): d for d in v2_docs}
    url_repo = AsyncMock()
    url_repo.find_by_alias = AsyncMock(
        side_effect=lambda alias, domain: v2_by_key.get((alias, domain))
    )

    v1_map = v1_docs or {}
    legacy_repo = AsyncMock()
    legacy_repo.aggregate = AsyncMock(
        side_effect=lambda pipeline: v1_map.get(pipeline[0]["$match"]["_id"])
    )

    emoji_map = emoji_docs or {}
    emoji_repo = AsyncMock()
    emoji_repo.aggregate = AsyncMock(
        side_effect=lambda pipeline: emoji_map.get(pipeline[0]["$match"]["_id"])
    )

    entitlements = AsyncMock()
    entitlements.resolve_for = AsyncMock(return_value=for_plan(plan))
    flags = AsyncMock()
    flags.is_enabled_for_owner = AsyncMock(return_value=flag_on)
    return PublicPreviewService(
        PublicLinkResolver(
            url_repo,
            legacy_repo,
            emoji_repo,
            system_default_domain="spoo.me",
        ),
        entitlements,
        flags,
    )


def _get(service: PublicPreviewService, code: str):
    application = _build_test_app({get_public_preview_service: lambda: service})
    with TestClient(application, raise_server_exceptions=False) as client:
        return client.get(f"/api/v1/public/preview/{code}")


# ── 404 ────────────────────────────────────────────────────────────────────────


def test_missing_code_returns_404_not_found():
    resp = _get(_make_service(), "noexist")

    assert resp.status_code == 404
    assert resp.json() == {"error": "short_code not found", "code": "not_found"}


# ── v2, active ─────────────────────────────────────────────────────────────────


def test_v2_active_plain_full_body():
    resp = _get(_make_service(v2_docs=(_make_v2(),)), "testme")

    assert resp.status_code == 200
    assert resp.json() == {
        "generation": "v2",
        "alias": "testme",
        "short_url": "https://spoo.me/testme",
        "whitelabel": False,
        "status": "active",
        "created_at": "2024-01-01T00:00:00+00:00",
        "starts_at": None,
        "password_protected": False,
        "destination": {
            "url": "https://example.com/long",
            "domain": "example.com",
            "path": "/long",
            "is_https": True,
        },
        "geo_destinations": None,
        "variant_destinations": None,
        "expired_destination": None,
    }


def test_v2_destination_bare_host_has_empty_path():
    doc = _make_v2(long_url="https://example.com/")
    resp = _get(_make_service(v2_docs=(doc,)), "testme")

    assert resp.status_code == 200
    assert resp.json()["destination"] == {
        "url": "https://example.com/",
        "domain": "example.com",
        "path": "",
        "is_https": True,
    }


def test_v2_destination_preserves_query_and_fragment():
    doc = _make_v2(long_url="http://sub.example.com/a/b?x=1&y=2#frag")
    resp = _get(_make_service(v2_docs=(doc,)), "testme")

    assert resp.status_code == 200
    assert resp.json()["destination"] == {
        "url": "http://sub.example.com/a/b?x=1&y=2#frag",
        "domain": "sub.example.com",
        "path": "/a/b?x=1&y=2#frag",
        "is_https": False,
    }


def test_v2_geo_rules_grouped_by_url_countries_sorted():
    doc = _make_v2(
        geo_rules={
            "US": "https://us.example.com/promo",
            "DE": "https://eu.example.com/",
            "CA": "https://us.example.com/promo",
        }
    )
    resp = _get(_make_service(v2_docs=(doc,)), "testme")

    assert resp.status_code == 200
    body = resp.json()
    # Every rule listed, grouped by destination URL, countries sorted.
    assert body["geo_destinations"] == [
        {
            "countries": ["CA", "US"],
            "url": "https://us.example.com/promo",
            "domain": "us.example.com",
            "path": "/promo",
            "is_https": True,
        },
        {
            "countries": ["DE"],
            "url": "https://eu.example.com/",
            "domain": "eu.example.com",
            "path": "",
            "is_https": True,
        },
    ]
    # The plain destination stays the unmatched-country fallback long_url.
    assert body["destination"]["url"] == "https://example.com/long"


def test_v2_ab_variants_listed_with_weights():
    doc = _make_v2(
        ab_variants=[
            {"url": "https://b.example.com/promo", "weight": 60},
            {"url": "https://example.com/c", "weight": 20},
        ]
    )
    resp = _get(_make_service(v2_docs=(doc,)), "testme")

    assert resp.status_code == 200
    body = resp.json()
    assert body["variant_destinations"] == [
        {
            "weight": 60,
            "url": "https://b.example.com/promo",
            "domain": "b.example.com",
            "path": "/promo",
            "is_https": True,
        },
        {
            "weight": 20,
            "url": "https://example.com/c",
            "domain": "example.com",
            "path": "/c",
            "is_https": True,
        },
    ]
    assert body["destination"]["url"] == "https://example.com/long"


def test_v2_without_variants_has_null_variant_destinations():
    resp = _get(_make_service(v2_docs=(_make_v2(),)), "testme")
    assert resp.json()["variant_destinations"] is None


# ── v2, withholding ────────────────────────────────────────────────────────────


def test_v2_password_protected_withholds_destination_and_geo():
    doc = _make_v2(
        password="$argon2id$fakehash",
        geo_rules={"US": "https://us.example.com/"},
        ab_variants=[{"url": "https://b.example.com/", "weight": 50}],
    )
    resp = _get(_make_service(v2_docs=(doc,)), "testme")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "active"
    assert body["password_protected"] is True
    assert body["destination"] is None
    assert body["geo_destinations"] is None
    assert body["variant_destinations"] is None
    assert "b.example.com" not in resp.text
    # No password unlock exists on this wire — the hash must never leak.
    assert "argon2" not in resp.text


@pytest.mark.parametrize("status", ["EXPIRED", "INACTIVE", "BLOCKED"])
def test_v2_non_active_status_lowercased_and_destination_withheld(status):
    doc = _make_v2(status=status, geo_rules={"US": "https://us.example.com/"})
    resp = _get(_make_service(v2_docs=(doc,)), "testme")

    assert resp.status_code == 200  # status-agnostic resolution: still answers
    body = resp.json()
    assert body["status"] == status.lower()
    assert body["destination"] is None
    assert body["geo_destinations"] is None


def test_v2_active_with_past_expire_after_reads_expired():
    # Naive datetime, as pymongo returns them (naive == UTC).
    past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    doc = _make_v2(expire_after=past)
    resp = _get(_make_service(v2_docs=(doc,)), "testme")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "expired"
    assert body["destination"] is None


def test_v2_expired_with_fallback_names_where_visitors_go():
    doc = _make_v2(
        max_clicks=5,
        total_clicks=5,
        expired_redirect_url="https://ended.example/bye?x=1",
    )
    body = _get(_make_service(v2_docs=(doc,)), "testme").json()

    assert body["status"] == "expired"
    assert body["destination"] is None
    assert body["expired_destination"] == {
        "url": "https://ended.example/bye?x=1",
        "domain": "ended.example",
        "path": "/bye?x=1",
        "is_https": True,
    }


def test_v2_active_with_fallback_keeps_it_out_of_the_preview():
    doc = _make_v2(expired_redirect_url="https://ended.example/bye")
    body = _get(_make_service(v2_docs=(doc,)), "testme").json()

    assert body["status"] == "active"
    assert body["expired_destination"] is None


def test_v2_active_with_max_clicks_reached_reads_expired():
    doc = _make_v2(max_clicks=5, total_clicks=5)
    resp = _get(_make_service(v2_docs=(doc,)), "testme")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "expired"
    assert body["destination"] is None


# ── v1 / emoji ─────────────────────────────────────────────────────────────────


def test_v1_active_full_body():
    # A timezone-naive expiration is ambiguous and must NOT expire the link
    # (mirrors convert_to_gmt on the legacy stats page).
    doc = _make_v1(**{"expiration-time": "2000-01-01T00:00:00"})
    resp = _get(_make_service(v1_docs={"abc123": doc}), "abc123")

    assert resp.status_code == 200
    assert resp.json() == {
        "generation": "v1",
        "alias": "abc123",
        "short_url": "https://spoo.me/abc123",
        "whitelabel": False,
        "status": "active",
        "created_at": "2024-03-10T12:00:00+00:00",
        "starts_at": None,
        "password_protected": False,
        "destination": {
            "url": "https://example.com/page",
            "domain": "example.com",
            "path": "/page",
            "is_https": True,
        },
        "geo_destinations": None,
        "variant_destinations": None,
        "expired_destination": None,
    }


def test_v1_expiration_time_passed_reads_expired_and_withholds():
    doc = _make_v1(**{"expiration-time": "2020-01-01T00:00:00+00:00"})
    resp = _get(_make_service(v1_docs={"abc123": doc}), "abc123")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "expired"
    assert body["destination"] is None


def test_v1_max_clicks_reached_reads_expired_and_withholds():
    doc = _make_v1(**{"max-clicks": 10, "total-clicks": 10})
    resp = _get(_make_service(v1_docs={"abc123": doc}), "abc123")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "expired"
    assert body["destination"] is None


def test_v1_plaintext_password_withholds_destination():
    doc = _make_v1(password="hunter2@1")
    resp = _get(_make_service(v1_docs={"abc123": doc}), "abc123")

    assert resp.status_code == 200
    body = resp.json()
    assert body["password_protected"] is True
    assert body["destination"] is None
    # The plaintext password itself must never leak.
    assert "hunter2" not in resp.text


def test_v1_missing_creation_date_gives_null_created_at():
    doc = _make_v1()
    del doc["creation-date"]
    resp = _get(_make_service(v1_docs={"abc123": doc}), "abc123")

    assert resp.status_code == 200
    body = resp.json()
    assert body["created_at"] is None
    assert body["generation"] == "v1"


def test_v1_malformed_destination_url_still_answers_200():
    # Raw legacy `url` values can be garbage urlparse refuses (unclosed
    # IPv6 bracket raises ValueError) — a public endpoint must not 500.
    doc = _make_v1(url="http://[::1")
    resp = _get(_make_service(v1_docs={"abc123": doc}), "abc123")

    assert resp.status_code == 200
    assert resp.json()["destination"] == {
        "url": "http://[::1",
        "domain": "http:",
        "path": "",
        "is_https": False,
    }


def test_emoji_alias_percent_encoded_resolves_as_v1():
    doc = _make_v1(_id="🚀✨", url="https://docs.spoo.me/emoji-urls")
    resp = _get(
        _make_service(emoji_docs={"🚀✨": doc}),
        "%F0%9F%9A%80%E2%9C%A8",
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["generation"] == "v1"
    assert body["alias"] == "🚀✨"
    assert body["short_url"] == "https://spoo.me/🚀✨"
    assert body["destination"]["domain"] == "docs.spoo.me"


# ── Fields that must never influence / ride the wire ───────────────────────────


def test_private_stats_link_previews_normally():
    doc = _make_v2(private_stats=True)
    resp = _get(_make_service(v2_docs=(doc,)), "testme")

    assert resp.status_code == 200
    assert resp.json()["destination"] is not None  # preview ≠ stats


def test_owner_meta_tags_never_on_the_wire():
    doc = _make_v2(meta_tags={"title": "Owner Bait Title", "description": "Owner copy"})
    resp = _get(_make_service(v2_docs=(doc,)), "testme")

    assert resp.status_code == 200
    body = resp.json()
    assert "meta_tags" not in body
    assert "title" not in body
    assert "Owner Bait Title" not in resp.text
    # The link itself previews normally — omission isn't withholding.
    assert body["destination"]["url"] == "https://example.com/long"


# ── Resolution behavior ────────────────────────────────────────────────────────


def test_lookups_are_case_sensitive():
    service = _make_service(v2_docs=(_make_v2(alias="Docs"),))

    hit = _get(service, "Docs")
    assert hit.status_code == 200
    assert hit.json()["alias"] == "Docs"

    miss = _get(service, "docs")
    assert miss.status_code == 404


def test_six_char_code_in_both_generations_resolves_v1_first():
    service = _make_service(
        v2_docs=(_make_v2(alias="sixsix"),),
        v1_docs={"sixsix": _make_v1(_id="sixsix")},
    )
    resp = _get(service, "sixsix")

    assert resp.status_code == 200
    assert resp.json()["generation"] == "v1"


def test_seven_char_code_in_both_generations_resolves_v2_first():
    service = _make_service(
        v2_docs=(_make_v2(alias="sevens7"),),
        v1_docs={"sevens7": _make_v1(_id="sevens7")},
    )
    resp = _get(service, "sevens7")

    assert resp.status_code == 200
    assert resp.json()["generation"] == "v2"


def test_v2_scheduled_withholds_destination_and_carries_start():
    starts = datetime.now(timezone.utc) + timedelta(days=2)
    doc = _make_v2(starts_at=starts)
    resp = _get(_make_service(v2_docs=(doc,)), "testme")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "scheduled"
    assert body["destination"] is None
    assert body["geo_destinations"] is None
    assert body["starts_at"] == int(starts.timestamp())


def test_v2_started_reads_active_without_start():
    doc = _make_v2(starts_at=datetime.now(timezone.utc) - timedelta(days=2))
    resp = _get(_make_service(v2_docs=(doc,)), "testme")

    body = resp.json()
    assert body["status"] == "active"
    assert body["starts_at"] is None
    assert body["destination"] is not None


def test_whitelabel_needs_the_flag_and_the_entitlement():
    doc = _make_v2()
    assert (
        _get(_make_service((doc,), plan=Plan.PRO), "testme").json()["whitelabel"]
        is False
    )
    assert (
        _get(_make_service((doc,), plan=Plan.PRO, flag_on=True), "testme").json()[
            "whitelabel"
        ]
        is True
    )
    assert (
        _get(_make_service((doc,), flag_on=True), "testme").json()["whitelabel"]
        is False
    )
