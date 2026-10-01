"""Payload builders — internal facts → public payloads, privacy guarantees."""

from __future__ import annotations

from datetime import datetime, timezone

from bson import ObjectId

from app.infrastructure.cache.url_cache import UrlCacheData
from app.schemas.models.base import ANONYMOUS_OWNER_ID
from app.schemas.models.url import UrlV2Doc
from app.services.click.events import ClickEvent
from app.services.webhooks.payloads import (
    build_link_clicked,
    build_link_expired,
    link_snapshot,
)
from app.services.webhooks.registry import validate_payload

_OWNER = ObjectId()


def _doc(owner_id: ObjectId = _OWNER) -> UrlV2Doc:
    doc = UrlV2Doc(
        alias="drop",
        owner_id=owner_id,
        domain="spoo.me",
        created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        long_url="https://example.com/x",
        password="argon2-hash-material",
        max_clicks=100,
        total_clicks=100,
    )
    doc.id = ObjectId()
    return doc


def _click_event(user_agent: str = "Mozilla/5.0 (X11; Linux x86_64)") -> ClickEvent:
    return ClickEvent(
        short_code="drop",
        schema_key="v2",
        is_emoji=False,
        url=UrlCacheData(
            _id=str(ObjectId()),
            alias="drop",
            long_url="https://example.com/x",
            block_bots=False,
            password_hash=None,
            expiration_time=None,
            max_clicks=None,
            url_status="ACTIVE",
            schema_version="v2",
            owner_id=str(_OWNER),
            domain="spoo.me",
        ),
        client_ip="203.0.113.9",
        user_agent=user_agent,
        referrer=None,
        cf_city=None,
        redirect_ms=4,
    )


class TestLinkSnapshot:
    def test_password_becomes_presence_boolean(self):
        snap = link_snapshot(_doc())
        assert snap["password_protected"] is True
        assert "password" not in snap
        assert "argon2" not in str(snap)

    def test_snapshot_keys_match_the_registry_model(self):
        """The registry drives the webhook docs; a snapshot key it does not
        declare would ship undocumented."""
        from app.services.webhooks.registry import LinkSnapshot, _sample_link

        declared = set(LinkSnapshot.model_fields)
        assert set(link_snapshot(_doc())) <= declared
        assert set(_sample_link()) == declared


class TestLinkExpired:
    def test_anonymous_owner_returns_none(self):
        assert build_link_expired(_doc(owner_id=ANONYMOUS_OWNER_ID), "x") is None

    def test_owned_link_builds_valid_payload(self):
        event = build_link_expired(_doc(), "max_clicks_reached")
        assert event is not None
        assert event.type == "link.expired"
        assert event.owner_id == str(_OWNER)
        assert event.data["reason"] == "max_clicks_reached"
        validate_payload("link.expired", event.data)


class TestLinkClicked:
    async def test_builds_valid_payload_with_no_ip(self):
        event = await build_link_clicked(_click_event(), "spoo.me")
        assert event is not None
        validate_payload("link.clicked", event.data)
        assert "203.0.113.9" not in str(event.data)
        assert event.data["is_bot"] is False
        assert event.data["user_agent"] == "Mozilla/5.0 (X11; Linux x86_64)"

    async def test_user_agent_bounded_and_stripped(self):
        crafted = "Mozilla/5.0 \x00\x1b[31m" + "A" * 2000
        event = await build_link_clicked(_click_event(user_agent=crafted), "spoo.me")
        assert event is not None
        ua = event.data["user_agent"]
        assert len(ua) <= 512
        assert "\x00" not in ua and "\x1b" not in ua

    async def test_geoip_fallback_is_awaited(self):
        """The geoip country fallback is async; a regression to an
        unawaited call would leak a coroutine into the payload."""
        from unittest.mock import AsyncMock

        geoip = AsyncMock()
        geoip.get_country_code = AsyncMock(return_value="DE")
        event = await build_link_clicked(_click_event(), "spoo.me", geoip=geoip)
        assert event.data["country"] == "DE"
        validate_payload("link.clicked", event.data)

    async def test_bot_click_flagged_not_suppressed(self):
        event = await build_link_clicked(_click_event(user_agent="curl/8.0"), "spoo.me")
        assert event is not None
        assert event.data["is_bot"] is True


class TestEventChanges:
    def test_meta_tags_change_strips_internal_fields(self):
        from app.schemas.models.url import LinkMetaTags
        from app.services.webhooks.payloads import event_changes

        existing = _doc()
        update_ops = {
            "meta_tags": {
                "title": "New",
                "description": None,
                "image": None,
                "color": "#112233",
                "image_meta": None,
                "updated_at": datetime.now(timezone.utc),
                "updated_ip": "203.0.113.9",
            },
            "password": "new-argon2-hash",
            "expire_after": datetime(2026, 8, 1, tzinfo=timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
        changes = event_changes(existing, update_ops)

        assert "updated_at" not in changes
        assert changes["meta_tags"]["new"] == {
            "title": "New",
            "description": None,
            "image": None,
            "color": "#112233",
        }
        assert "203.0.113.9" not in str(changes)
        assert "argon2" not in str(changes)
        assert changes["password_protected"] == {"old": True, "new": True}
        # Public vocabulary on the wire: internal expire_after surfaces as
        # the snapshot's expires_at, never the Mongo field name.
        assert "expire_after" not in changes
        assert changes["expires_at"]["new"] == "2026-08-01T00:00:00+00:00"
        # old side of a model-typed field sanitizes the same way
        existing_with_meta = existing.model_copy(
            update={"meta_tags": LinkMetaTags(title="Old", updated_ip="198.51.100.7")}
        )
        changes2 = event_changes(existing_with_meta, update_ops)
        assert changes2["meta_tags"]["old"]["title"] == "Old"
        assert "198.51.100.7" not in str(changes2)


def test_link_snapshot_carries_schedule_fields():
    from app.services.webhooks.payloads import link_snapshot

    doc = _doc()
    assert link_snapshot(doc)["starts_at"] is None
    assert link_snapshot(doc)["pre_start_url"] is None

    scheduled = doc.model_copy(
        update={
            "starts_at": datetime(2030, 1, 1, tzinfo=timezone.utc),
            "pre_start_url": "https://example.org/soon",
        }
    )
    snap = link_snapshot(scheduled)
    assert snap["starts_at"] == "2030-01-01T00:00:00+00:00"
    assert snap["pre_start_url"] == "https://example.org/soon"
