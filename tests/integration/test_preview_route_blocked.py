"""One real request per branch of GET /{code}+.

The route resolves a link through four branches (emoji, six-char v1,
six-char v2 fallback, long-alias v2, long-alias v1 fallback) and each
builds its own dict. A leak fix that patches three of them leaves a
hole, and a property called as a method 500s the whole page, so every
branch gets a request here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from bson import ObjectId
from fastapi.testclient import TestClient

from config import AppSettings
from dependencies import get_db, get_settings
from routes.legacy.url_shortener import router as legacy_url_router
from schemas.models.url import EmojiUrlDoc, LegacyUrlDoc, UrlStatus, UrlV2Doc
from tests.conftest import build_test_app

_DEST = "https://credential-harvest.example/login"
_GEO = {"IN": "https://geo-harvest.example/in"}


def _v2(alias: str, *, status: UrlStatus) -> UrlV2Doc:
    return UrlV2Doc(
        _id=ObjectId(),
        alias=alias,
        owner_id=ObjectId(),
        domain="spoo.me",
        created_at=datetime.now(timezone.utc),
        long_url=_DEST,
        geo_rules=_GEO,
        status=status,
    )


def _client(*, v2=None, v1=None, emoji=None) -> TestClient:
    db = MagicMock()
    app = build_test_app(
        legacy_url_router,
        overrides={get_db: lambda: db, get_settings: lambda: AppSettings()},
    )

    async def find_by_alias(alias, domain):
        return v2

    async def find_v1(code):
        return v1

    async def find_emoji(alias):
        return emoji

    import routes.legacy.url_shortener as mod

    mod.UrlRepository = lambda _c: MagicMock(
        find_by_alias=AsyncMock(side_effect=find_by_alias)
    )
    mod.LegacyUrlRepository = lambda _c: MagicMock(
        find_by_id=AsyncMock(side_effect=find_v1)
    )
    mod.EmojiUrlRepository = lambda _c: MagicMock(
        find_by_id=AsyncMock(side_effect=find_emoji)
    )
    return TestClient(app)


class TestPreviewServesActiveLinks:
    def test_six_char_v2_previews_without_a_500(self):
        """The property-called-as-a-method regression: every v2 code 500s."""
        with _client(v2=_v2("abc123", status=UrlStatus.ACTIVE)) as client:
            resp = client.get("/abc123+")
        assert resp.status_code == 200
        assert "credential-harvest" in resp.text

    def test_long_alias_v2_previews_without_a_500(self):
        with _client(v2=_v2("longalias7", status=UrlStatus.ACTIVE)) as client:
            resp = client.get("/longalias7+")
        assert resp.status_code == 200
        assert "credential-harvest" in resp.text


class TestPreviewRefusesBlockedLinks:
    def test_six_char_v2_blocked(self):
        with _client(v2=_v2("abc123", status=UrlStatus.BLOCKED)) as client:
            resp = client.get("/abc123+")
        assert resp.status_code == 451
        assert "credential-harvest" not in resp.text
        assert "geo-harvest" not in resp.text

    def test_long_alias_v2_blocked_hides_geo_destinations_too(self):
        with _client(v2=_v2("longalias7", status=UrlStatus.BLOCKED)) as client:
            resp = client.get("/longalias7+")
        assert resp.status_code == 451
        assert "credential-harvest" not in resp.text
        assert "geo-harvest" not in resp.text

    def test_six_char_v1_blocked(self):
        doc = LegacyUrlDoc(_id="abc123", url=_DEST, blocked=True)
        with _client(v1=doc) as client:
            resp = client.get("/abc123+")
        assert resp.status_code == 451
        assert "credential-harvest" not in resp.text

    def test_long_alias_v1_fallback_blocked(self):
        """The branch the first leak fix missed: v2 misses, v1 answers."""
        doc = LegacyUrlDoc(_id="longalias7", url=_DEST, blocked=True)
        with _client(v2=None, v1=doc) as client:
            resp = client.get("/longalias7+")
        assert resp.status_code == 451
        assert "credential-harvest" not in resp.text

    def test_emoji_blocked(self):
        doc = EmojiUrlDoc(_id="⭐🎉", url=_DEST, blocked=True)
        with _client(emoji=doc) as client:
            resp = client.get("/⭐🎉+")
        assert resp.status_code == 451
        assert "credential-harvest" not in resp.text
