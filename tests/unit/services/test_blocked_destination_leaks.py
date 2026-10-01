"""A blocked link must not reveal its destination on ANY surface.

Seven wire surfaces leaked it: the legacy stats page and its JSON twin,
legacy export in four formats, the legacy preview page (which also
enumerated the whole geo_rules map), and the public preview and stats
APIs for v1/emoji. Each root cause gets a test here so a new surface
inherits the fix instead of repeating the bug.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bson import ObjectId

from app.schemas.models.url import EmojiUrlDoc, LegacyUrlDoc, UrlStatus, UrlV2Doc
from app.services.public_link_resolver import ResolvedPublicLink, SchemaVersion


class TestLegacyModelFunnel:
    def test_blocked_flag_reads_as_blocked_status(self):
        doc = LegacyUrlDoc(_id="abc123", url="https://evil.example/kit", blocked=True)
        assert doc.effective_status is UrlStatus.BLOCKED

    def test_absent_flag_is_active(self):
        doc = LegacyUrlDoc(_id="abc123", url="https://ok.example/")
        assert doc.effective_status is UrlStatus.ACTIVE

    def test_emoji_inherits_the_funnel(self):
        doc = EmojiUrlDoc(_id="⭐🎉", url="https://evil.example/kit", blocked=True)
        assert doc.effective_status is UrlStatus.BLOCKED


class TestPublicResolverStatus:
    def _link(self, **raw) -> ResolvedPublicLink:
        return ResolvedPublicLink(
            schema=SchemaVersion.V1,
            alias="abc123",
            short_url="https://spoo.me/abc123",
            v2_doc=None,
            raw_v1={"_id": "abc123", "url": "https://evil.example/kit", **raw},
        )

    def test_blocked_v1_reports_blocked(self):
        assert self._link(blocked=True).effective_status() == "blocked"

    def test_unblocked_v1_still_active(self):
        assert self._link().effective_status() == "active"


class TestLegacyStatsProjection:
    def test_pipeline_projects_the_blocked_flag(self):
        """Without this the downstream endpoints cannot check at all."""
        from app.routes.legacy.helpers import get_stats_pipeline

        project = next(
            stage["$project"]
            for stage in get_stats_pipeline("abc123")
            if "$project" in stage
        )
        assert "blocked" in project
        assert project["blocked"] == {"$ifNull": ["$blocked", False]}


class TestScheduledWithheld:
    """A scheduled link's destination was never public. Every surface that
    withholds for BLOCKED must withhold for SCHEDULED too; the legacy preview
    route test covers the page itself."""

    def _doc(self) -> UrlV2Doc:
        return UrlV2Doc.from_mongo(
            {
                "_id": ObjectId(),
                "alias": "sched01",
                "domain": "spoo.me",
                "created_at": datetime.now(timezone.utc),
                "long_url": "https://secret.example/launch",
                "starts_at": datetime.now(timezone.utc) + timedelta(days=1),
            }
        )

    def test_future_start_reads_as_scheduled(self):
        assert self._doc().effective_status is UrlStatus.SCHEDULED

    def test_public_resolver_reports_scheduled(self):
        link = ResolvedPublicLink(
            schema=SchemaVersion.V2,
            alias="sched01",
            short_url="https://spoo.me/sched01",
            v2_doc=self._doc(),
        )
        assert link.effective_status() == "scheduled"

    def test_public_preview_withholds_destination(self):
        from app.services.public_preview_service import PublicPreviewService

        svc = PublicPreviewService.__new__(PublicPreviewService)
        link = ResolvedPublicLink(
            schema=SchemaVersion.V2,
            alias="sched01",
            short_url="https://spoo.me/sched01",
            v2_doc=self._doc(),
        )
        body = svc._v2_preview(link)
        assert body.status == "scheduled"
        assert body.destination is None
        assert body.geo_destinations is None
        assert "secret.example" not in body.model_dump_json()
