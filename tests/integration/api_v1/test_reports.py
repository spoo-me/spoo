"""Tests for POST /api/v1/reports — bulk-first report intake.

The service under test is REAL end to end: real ReportIntakeService,
real Report/ReportSubmission repositories over capturing fake
collections (so the $inc/$addToSet upsert shape is pinned at the Mongo
boundary), and the real PublicLinkResolver over dict-backed repo fakes
(so generation dispatch + emoji decoding run for real). Only the
captcha, the notifier's HTTP client, and settings are injected — summary
embeds asserted here are the exact bodies Discord would receive.

Wire shapes here are FROZEN — the Next report page builds against the
exact bodies asserted in this file.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

from config import AppSettings
from dependencies import get_current_user
from dependencies.services import get_report_intake_service
from infrastructure.ops_notify import DiscordOpsNotifier
from middleware.rate_limiter import limiter
from repositories.report_repository import (
    ReportRepository,
    ReportSubmissionRepository,
)
from routes.api_v1 import router as api_v1_router
from schemas.models.url import UrlV2Doc
from services.public_link_resolver import PublicLinkResolver
from services.report_intake_service import (
    ReportIntakeService,
    normalize_report_target,
)
from tests.conftest import build_test_app

from .conftest import _make_api_key_doc, _make_user

_URL = "/api/v1/reports"
_DOMAIN = "spoo.me"
_SUBMISSION_OID = ObjectId("65e000000000000000000001")


@pytest.fixture(autouse=True)
def _reset_limiter_between_tests():
    """REPORTS_ANON is 5/min and every test posts anonymously from the
    same TestClient IP — clear the in-memory counters around each test."""
    limiter.reset()
    yield
    limiter.reset()


# ── Fakes ─────────────────────────────────────────────────────────────────────


class _FakeReportsCollection:
    """Captures raw update ops so the dedupe+velocity upsert shape is
    asserted exactly. Deliberately has NO insert method: a regression to
    insert-per-report fails loudly."""

    name = "reports"

    def __init__(self) -> None:
        self.update_calls: list[tuple[dict, dict, bool]] = []

    async def update_one(self, flt: dict, ops: dict, upsert: bool = False):
        self.update_calls.append((flt, ops, upsert))
        return SimpleNamespace(matched_count=1, modified_count=1, upserted_id=None)


class _FakeSubmissionsCollection:
    name = "report_submissions"

    def __init__(self) -> None:
        self.inserted: list[dict] = []

    async def insert_one(self, doc: dict):
        self.inserted.append(doc)
        return SimpleNamespace(inserted_id=_SUBMISSION_OID)


class _CapturingHttp:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.payloads: list[dict[str, Any]] = []

    async def post(self, url: str, json: dict[str, Any]):
        self.payloads.append(json)
        return SimpleNamespace(status_code=204 if self.ok else 500, text="boom")


class _FakeNotifier(DiscordOpsNotifier):
    """The REAL DiscordOpsNotifier over a capturing HTTP fake, so the
    summary embed shape is pinned end to end."""

    def __init__(self, ok: bool = True) -> None:
        self._captured = _CapturingHttp(ok)
        super().__init__("", "https://hooks.test/report", self._captured)

    @property
    def payloads(self) -> list[dict[str, Any]]:
        return self._captured.payloads


class _FakeCaptcha:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.tokens: list[str] = []

    async def verify(self, token: str) -> bool:
        self.tokens.append(token)
        return self.ok


class _DictUrlRepo:
    """Stand-in for UrlRepository — (alias, domain)-keyed lookups. Serves
    both the resolver (system domain) and the service's custom-domain path."""

    def __init__(self, docs: list[UrlV2Doc] | None = None) -> None:
        self._docs = {(doc.alias, doc.domain): doc for doc in (docs or [])}

    async def find_by_alias(self, alias: str, domain: str) -> UrlV2Doc | None:
        return self._docs.get((alias, domain))


class _DictLegacyRepo:
    """Stand-in for Legacy/EmojiUrlRepository — raw-dict aggregate reads,
    the same surface PublicLinkResolver uses in production."""

    def __init__(self, docs: dict[str, dict[str, Any]] | None = None) -> None:
        self._docs = docs or {}

    async def aggregate(self, pipeline: list[dict[str, Any]]) -> dict[str, Any] | None:
        return self._docs.get(pipeline[0]["$match"]["_id"])


# ── Builders ──────────────────────────────────────────────────────────────────


def _make_v2_doc(alias: str, domain: str = _DOMAIN) -> UrlV2Doc:
    return UrlV2Doc(
        **{
            "_id": ObjectId(),
            "alias": alias,
            "owner_id": ObjectId(),
            "domain": domain,
            "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
            "long_url": "https://example.com/long",
            "status": "ACTIVE",
            "total_clicks": 0,
        }
    )


class _CapturingSafetySink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


def _build_service(
    *,
    v2_docs: list[UrlV2Doc] | None = None,
    v1_docs: dict[str, dict[str, Any]] | None = None,
    emoji_docs: dict[str, dict[str, Any]] | None = None,
    captcha: _FakeCaptcha | None = None,
    notifier: _FakeNotifier | None = None,
    safety_sink: _CapturingSafetySink | None = None,
) -> tuple[ReportIntakeService, _FakeReportsCollection, _FakeSubmissionsCollection]:
    reports_col = _FakeReportsCollection()
    submissions_col = _FakeSubmissionsCollection()
    url_repo = _DictUrlRepo(v2_docs)
    resolver = PublicLinkResolver(
        url_repo,
        _DictLegacyRepo(v1_docs),
        _DictLegacyRepo(emoji_docs),
        system_default_domain=_DOMAIN,
    )
    service = ReportIntakeService(
        ReportRepository(reports_col),
        ReportSubmissionRepository(submissions_col),
        resolver,
        url_repo,
        captcha if captcha is not None else _FakeCaptcha(),
        notifier if notifier is not None else _FakeNotifier(),
        system_default_domain=_DOMAIN,
        safety_sink=safety_sink,
    )
    return service, reports_col, submissions_col


def _client(
    service: ReportIntakeService,
    *,
    user: Any = None,
    sitekey: str = "",
    report_webhook: str = "https://hooks.test/report",
) -> TestClient:
    settings = AppSettings(
        url_report_webhook=report_webhook,
        hcaptcha_sitekey=sitekey,
    )
    app = build_test_app(
        api_v1_router,
        overrides={
            get_report_intake_service: lambda: service,
            get_current_user: lambda: user,
        },
        extra_state={"settings": settings},
    )
    return TestClient(app)


def _items(*codes: str, reason: str = "phishing") -> list[dict[str, Any]]:
    return [{"code_or_url": code, "reason": reason} for code in codes]


# ── Normalization (pure function) ─────────────────────────────────────────────


class TestNormalizeReportTarget:
    def test_forms(self):
        cases = {
            "abc1234": (None, "abc1234"),
            "spoo.me/abc1234": (None, "abc1234"),
            "https://spoo.me/abc1234?x=1#frag": (None, "abc1234"),
            "http://SPOO.ME/CaseKept": (None, "CaseKept"),
            "spoo.me:443/withport": (None, "withport"),
            "spoo.me./trailingdot": (None, "trailingdot"),
            # www serves the same site as the apex (Caddy vhost pair)
            "www.spoo.me/wwwform": (None, "wwwform"),
            "https://WWW.spoo.me/wwwform": (None, "wwwform"),
            "go.Customer.com/Deal": ("go.customer.com", "Deal"),
            "%F0%9F%98%80": (None, "😀"),
            "https://spoo.me/%F0%9F%98%80": (None, "😀"),
        }
        for raw, expected in cases.items():
            assert normalize_report_target(raw, _DOMAIN) == expected, raw

    def test_unparseable(self):
        for raw in (
            "",
            "   ",
            "spoo.me/",
            "spoo.me/stats/abc",
            "ftp://spoo.me/abc",
            "https:///nohost",
        ):
            assert normalize_report_target(raw, _DOMAIN) is None, raw


# ── Single item happy path ────────────────────────────────────────────────────


def test_single_item_wire_shape_storage_and_summary_notification():
    notifier = _FakeNotifier()
    service, reports_col, submissions_col = _build_service(
        v2_docs=[_make_v2_doc("abc1234")], notifier=notifier
    )
    with _client(service) as c:
        resp = c.post(_URL, json={"items": _items("abc1234")})

    # Frozen wire shape.
    assert resp.status_code == 200
    assert resp.json() == {
        "submission_id": str(_SUBMISSION_OID),
        "accepted": 1,
        "rejected": [],
    }

    # Storage: ONE upsert with the $inc/$addToSet velocity shape — never
    # an insert-per-report.
    assert len(reports_col.update_calls) == 1
    flt, ops, upsert = reports_col.update_calls[0]
    assert upsert is True
    assert flt == {"domain": None, "code": "abc1234"}
    assert ops["$inc"] == {
        "count": 1,
        "reporters.anonymous": 1,
        "source_counts.web": 1,
    }
    assert ops["$addToSet"] == {"reasons": "phishing"}
    assert ops["$setOnInsert"]["status"] == "open"
    assert ops["$setOnInsert"]["domain"] is None
    assert ops["$setOnInsert"]["code"] == "abc1234"
    assert "first_reported_at" in ops["$setOnInsert"]
    assert "last_reported_at" in ops["$set"]
    assert "last_details" not in ops["$set"]

    # Submission audit record.
    assert len(submissions_col.inserted) == 1
    sub = submissions_col.inserted[0]
    assert sub["source"] == "web"
    assert sub["reporter_id"] is None
    assert sub["item_count"] == 1
    assert sub["accepted"] == 1
    assert sub["rejected_count"] == 0

    # Notification demoted to ONE summary embed per submission.
    assert len(notifier.payloads) == 1
    embed = notifier.payloads[0]["embeds"][0]
    assert embed["title"] == "New URL Report Submission"
    assert embed["color"] == 14177041
    field_names = [f["name"] for f in embed["fields"]]
    assert field_names == [
        "Submission ID",
        "Source",
        "Accepted / Rejected",
        "Reported Links",
        "IP Address",
    ]
    assert f"```{_SUBMISSION_OID}```" == embed["fields"][0]["value"]
    assert embed["fields"][1]["value"] == "```web · anonymous```"
    assert embed["fields"][2]["value"] == "```1 / 0```"
    assert "spoo.me/abc1234 — phishing" in embed["fields"][3]["value"]
    assert embed["footer"]["text"] == "spoo-me"
    # The embed carries the SAME instant the audit record was stamped with.
    assert embed["timestamp"] == sub["created_at"].isoformat()


def test_details_and_vector_stored():
    service, reports_col, _ = _build_service(v2_docs=[_make_v2_doc("abc1234")])
    with _client(service) as c:
        resp = c.post(
            _URL,
            json={
                "items": [
                    {
                        "code_or_url": "abc1234",
                        "reason": "spam",
                        "details": "arrived via a fake parcel text",
                        "vector": "sms",
                    }
                ]
            },
        )

    assert resp.status_code == 200
    _, ops, _ = reports_col.update_calls[0]
    assert ops["$addToSet"] == {"reasons": "spam", "vectors": "sms"}
    assert ops["$set"]["last_details"] == "arrived via a fake parcel text"


# ── Bulk semantics ────────────────────────────────────────────────────────────


def test_bulk_mixed_batch_per_item_breakdown():
    notifier = _FakeNotifier()
    service, reports_col, submissions_col = _build_service(
        v2_docs=[
            _make_v2_doc("abc1234"),
            _make_v2_doc("deal", domain="go.customer.com"),
        ],
        notifier=notifier,
    )
    items = [
        {"code_or_url": "abc1234", "reason": "phishing"},
        # Same (domain, code) after normalization → duplicate_in_batch.
        {"code_or_url": "https://spoo.me/abc1234?utm=x", "reason": "malware"},
        # Multi-segment path → invalid_input.
        {"code_or_url": "spoo.me/stats/abc", "reason": "spam"},
        # Misses every generation → not_found.
        {"code_or_url": "missing99", "reason": "other"},
        # Custom-domain short URL → accepted, domain-scoped.
        {"code_or_url": "go.customer.com/deal", "reason": "phishing"},
    ]
    with _client(service) as c:
        resp = c.post(_URL, json={"items": items})

    assert resp.status_code == 200
    assert resp.json() == {
        "submission_id": str(_SUBMISSION_OID),
        "accepted": 2,
        "rejected": [
            {
                "index": 1,
                "input": "https://spoo.me/abc1234?utm=x",
                "code": "duplicate_in_batch",
            },
            {"index": 2, "input": "spoo.me/stats/abc", "code": "invalid_input"},
            {"index": 3, "input": "missing99", "code": "not_found"},
        ],
    }

    # Rejected items never sink the accepted ones.
    assert [(flt["domain"], flt["code"]) for flt, _, _ in reports_col.update_calls] == [
        (None, "abc1234"),
        ("go.customer.com", "deal"),
    ]
    # The custom-domain record keeps the tenant fqdn.
    _, custom_ops, _ = reports_col.update_calls[1]
    assert custom_ops["$setOnInsert"]["domain"] == "go.customer.com"

    sub = submissions_col.inserted[0]
    assert sub["item_count"] == 5
    assert sub["accepted"] == 2
    assert sub["rejected_count"] == 3

    # One summary embed for the whole batch, never per item.
    assert len(notifier.payloads) == 1


def test_custom_domain_code_missing_is_not_found():
    service, reports_col, _ = _build_service(v2_docs=[])
    with _client(service) as c:
        resp = c.post(_URL, json={"items": _items("go.customer.com/nope")})

    assert resp.status_code == 200
    assert resp.json()["rejected"] == [
        {"index": 0, "input": "go.customer.com/nope", "code": "not_found"}
    ]
    assert reports_col.update_calls == []


def test_emoji_code_percent_decoded():
    service, reports_col, _ = _build_service(
        emoji_docs={"😀": {"_id": "😀", "url": "https://example.com"}}
    )
    with _client(service) as c:
        resp = c.post(_URL, json={"items": _items("https://spoo.me/%F0%9F%98%80")})

    assert resp.status_code == 200
    assert resp.json()["accepted"] == 1
    flt, _, _ = reports_col.update_calls[0]
    assert flt == {"domain": None, "code": "😀"}


def test_re_report_increments_the_same_document():
    service, reports_col, _ = _build_service(v2_docs=[_make_v2_doc("abc1234")])
    with _client(service) as c:
        first = c.post(_URL, json={"items": _items("abc1234")})
        second = c.post(_URL, json={"items": _items("abc1234", reason="malware")})

    assert first.status_code == 200
    assert second.status_code == 200
    # Two UPSERTS against the same (domain, code) — the fake collection
    # has no insert method, so an insert-per-report regression cannot pass.
    assert len(reports_col.update_calls) == 2
    for flt, ops, upsert in reports_col.update_calls:
        assert upsert is True
        assert flt == {"domain": None, "code": "abc1234"}
        assert ops["$inc"]["count"] == 1
    # The re-report contributes its reason to the triage-hint set.
    assert reports_col.update_calls[1][1]["$addToSet"] == {"reasons": "malware"}


def test_summary_embed_clips_long_lines_and_reports_overflow():
    # 11 accepted items: the first has an oversized code (line-clip branch),
    # the 11th spills past the 10-code listing cap (overflow branch).
    long_code = "x" * 100
    codes = [long_code] + [f"bulk{i}" for i in range(10)]
    notifier = _FakeNotifier()
    service, _, _ = _build_service(
        v2_docs=[_make_v2_doc(code) for code in codes], notifier=notifier
    )
    with _client(service) as c:
        resp = c.post(_URL, json={"items": _items(*codes)})

    assert resp.status_code == 200
    assert resp.json()["accepted"] == 11

    embed = notifier.payloads[0]["embeds"][0]
    links_field = next(f for f in embed["fields"] if f["name"] == "Reported Links")
    lines = links_field["value"].strip("`").split("\n")
    # 10 listed codes + the overflow line.
    assert len(lines) == 11
    assert lines[10] == "… and 1 more"
    # The oversized line is clipped to the 80-char cap, ellipsis-terminated.
    full_line = f"spoo.me/{long_code} — phishing"
    assert lines[0] == full_line[:79] + "…"
    assert len(lines[0]) == 80
    # The in-budget lines are untouched.
    assert lines[1] == "spoo.me/bulk0 — phishing"


# ── Caps ──────────────────────────────────────────────────────────────────────


def test_empty_items_400():
    service, reports_col, _ = _build_service()
    with _client(service) as c:
        resp = c.post(_URL, json={"items": []})

    assert resp.status_code == 400
    assert resp.json()["code"] == "validation_error"
    assert reports_col.update_calls == []


def test_anonymous_over_25_items_400():
    notifier = _FakeNotifier()
    service, reports_col, _ = _build_service(notifier=notifier)
    with _client(service) as c:
        resp = c.post(_URL, json={"items": _items(*[f"code{i}" for i in range(26)])})

    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "validation_error"
    assert "25" in body["error"]
    # Whole request fails — nothing stored, nothing notified.
    assert reports_col.update_calls == []
    assert notifier.payloads == []


def test_authenticated_100_ok_101_400():
    service, _, submissions_col = _build_service()
    user = _make_user()
    with _client(service, user=user) as c:
        ok = c.post(_URL, json={"items": _items(*[f"c{i}" for i in range(100)])})
        over = c.post(_URL, json={"items": _items(*[f"c{i}" for i in range(101)])})

    assert ok.status_code == 200
    body = ok.json()
    assert body["accepted"] + len(body["rejected"]) == 100
    assert submissions_col.inserted[0]["item_count"] == 100

    assert over.status_code == 400
    assert over.json()["code"] == "validation_error"
    assert "100" in over.json()["error"]


# ── Captcha semantics ─────────────────────────────────────────────────────────


def test_anonymous_missing_captcha_when_configured_400():
    captcha = _FakeCaptcha()
    service, reports_col, _ = _build_service(captcha=captcha)
    with _client(service, sitekey="test-sitekey") as c:
        resp = c.post(_URL, json={"items": _items("abc1234")})

    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "validation_error"
    assert "captcha" in body["error"].lower()
    assert captcha.tokens == []
    assert reports_col.update_calls == []


def test_anonymous_captcha_failure_403():
    service, reports_col, _ = _build_service(
        v2_docs=[_make_v2_doc("abc1234")], captcha=_FakeCaptcha(ok=False)
    )
    with _client(service, sitekey="test-sitekey") as c:
        resp = c.post(_URL, json={"items": _items("abc1234"), "captcha_token": "bad"})

    assert resp.status_code == 403
    assert resp.json() == {
        "error": "Invalid captcha, please try again",
        "code": "forbidden",
    }
    assert reports_col.update_calls == []


def test_authenticated_skips_captcha():
    captcha = _FakeCaptcha()
    service, _, submissions_col = _build_service(
        v2_docs=[_make_v2_doc("abc1234")], captcha=captcha
    )
    with _client(service, user=_make_user(), sitekey="test-sitekey") as c:
        resp = c.post(_URL, json={"items": _items("abc1234")})

    assert resp.status_code == 200
    # Never verified — not even with an empty token.
    assert captcha.tokens == []
    assert submissions_col.inserted[0]["reporter_id"] is not None


# ── Auth, scopes, and source attribution ──────────────────────────────────────


def test_api_key_without_reports_scope_403():
    service, reports_col, _ = _build_service()
    user = _make_user(api_key_doc=_make_api_key_doc(scopes=["stats:read"]))
    with _client(service, user=user) as c:
        resp = c.post(_URL, json={"items": _items("abc1234")})

    assert resp.status_code == 403
    assert resp.json()["code"] == "forbidden"
    assert reports_col.update_calls == []


@pytest.mark.parametrize("scopes", [["reports:create"], ["admin:all"]])
def test_api_key_with_reports_scope_stores_as_api_source(scopes):
    service, reports_col, submissions_col = _build_service(
        v2_docs=[_make_v2_doc("abc1234")]
    )
    user = _make_user(api_key_doc=_make_api_key_doc(scopes=scopes))
    with _client(service, user=user) as c:
        resp = c.post(_URL, json={"items": _items("abc1234")})

    assert resp.status_code == 200
    _, ops, _ = reports_col.update_calls[0]
    assert ops["$inc"]["source_counts.api"] == 1
    assert ops["$inc"]["reporters.authenticated"] == 1
    assert ops["$addToSet"]["reporter_ids"] == user.user_id
    assert submissions_col.inserted[0]["source"] == "api"


def test_session_user_stores_as_web_source_with_reporter_id():
    service, reports_col, submissions_col = _build_service(
        v2_docs=[_make_v2_doc("abc1234")]
    )
    user = _make_user()
    with _client(service, user=user) as c:
        resp = c.post(
            _URL,
            json={
                "items": _items("abc1234"),
                "reporter_email": "desk@example.org",
                "reporter_org": "Example CERT",
            },
        )

    assert resp.status_code == 200
    _, ops, _ = reports_col.update_calls[0]
    assert ops["$inc"]["source_counts.web"] == 1
    sub = submissions_col.inserted[0]
    assert sub["source"] == "web"
    assert sub["reporter_id"] == user.user_id
    assert sub["reporter_email"] == "desk@example.org"
    assert sub["reporter_org"] == "Example CERT"


# ── Not configured / notification resilience ──────────────────────────────────


def test_report_webhook_unset_503():
    service, reports_col, _ = _build_service()
    with _client(service, report_webhook="") as c:
        resp = c.post(_URL, json={"items": _items("abc1234")})

    assert resp.status_code == 503
    body = resp.json()
    assert body["code"] == "not_configured"
    assert body["error"]
    assert reports_col.update_calls == []


def test_notify_send_failure_does_not_fail_the_submission():
    # Storage is the system of record; the notifier is only a notification.
    service, reports_col, _ = _build_service(
        v2_docs=[_make_v2_doc("abc1234")], notifier=_FakeNotifier(ok=False)
    )
    with _client(service) as c:
        resp = c.post(_URL, json={"items": _items("abc1234")})

    assert resp.status_code == 200
    assert resp.json()["accepted"] == 1
    assert len(reports_col.update_calls) == 1


# ── Rate limiting ─────────────────────────────────────────────────────────────


def test_anonymous_rate_limit_smoke():
    service, _, _ = _build_service(v2_docs=[_make_v2_doc("abc1234")])
    with _client(service) as c:
        statuses = [
            c.post(_URL, json={"items": _items("abc1234")}).status_code
            for _ in range(6)
        ]

    # REPORTS_ANON = "5 per minute; 40 per day" — the 6th submission trips.
    assert statuses[:5] == [200] * 5
    assert statuses[5] == 429


# ── Safety enqueue ────────────────────────────────────────────────────────────


def test_accepted_reports_enqueue_safety_analysis_deduped_by_host():
    """Each accepted report enqueues one analysis request per distinct
    destination host, with reasons and reported codes merged."""
    doc_a = _make_v2_doc("aaa1234")
    doc_a.long_url = "https://kit.evil.com/login1"
    doc_b = _make_v2_doc("bbb1234")
    doc_b.long_url = "https://kit.evil.com/login2"
    doc_c = _make_v2_doc("ccc1234")
    doc_c.long_url = "https://other.scam.net/x"
    sink = _CapturingSafetySink()
    service, _, _ = _build_service(v2_docs=[doc_a, doc_b, doc_c], safety_sink=sink)

    with _client(service) as c:
        resp = c.post(
            _URL,
            json={
                "items": [
                    {"code_or_url": "aaa1234", "reason": "phishing"},
                    {"code_or_url": "bbb1234", "reason": "malware"},
                    {"code_or_url": "ccc1234", "reason": "phishing"},
                ]
            },
        )
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 3

    by_host = {e.host: e for e in sink.events}
    assert set(by_host) == {"kit.evil.com", "other.scam.net"}
    merged = by_host["kit.evil.com"]
    assert merged.registrable_domain == "evil.com"
    assert merged.trigger == "report"
    assert set(merged.context["reasons"]) == {"phishing", "malware"}
    assert len(merged.context["reported_codes"]) == 2


def test_safety_sink_absent_means_no_enqueue_and_no_failure():
    service, reports_col, _ = _build_service(v2_docs=[_make_v2_doc("abc1234")])
    with _client(service) as c:
        resp = c.post(_URL, json={"items": _items("abc1234")})
    assert resp.status_code == 200
    assert len(reports_col.update_calls) == 1
