"""
ReportIntakeService — bulk-first abuse-report intake.

Owns the whole INTAKE half of the url-safety funnel: normalization
(bare code / full URL / custom domain → ``(domain, code)``),
domain-scoped existence checks, within-batch dedupe, dedupe+velocity
storage, the per-POST submission audit record, and the demoted operator
notification (ONE summary per submission, never per item — storage is
the system of record now, the ping is a notification).

Reporter-claimed ``reason``/``vector`` are stored verbatim as triage
hints; assessed harm tiers live in the url-safety architecture, not
here. Resolution / triage / status transitions are explicitly out of
scope — this service ends at the DB record and the notification.

Framework-agnostic: no FastAPI imports. The route layer owns HTTP
concerns (client IP, the webhook-URL-unset 503 gate, and the
missing-captcha-token 400 that mirrors the Jinja form).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

from bson import ObjectId

from app.errors import ForbiddenError, ValidationError
from app.infrastructure.captcha.protocol import CaptchaProvider
from app.infrastructure.logging import get_logger
from app.infrastructure.ops_notify import OpsNotifier
from app.repositories.report_repository import (
    ReportRepository,
    ReportSubmissionRepository,
)
from app.repositories.url_repository import UrlRepository
from app.schemas.dto.requests.reports import ReportItemRequest
from app.schemas.enums.report import RejectionCode
from app.services.public_link_resolver import PublicLinkResolver
from app.services.safety.events import SafetyAnalyzeEvent
from app.services.safety.sinks import SafetySink
from app.shared.url_utils import (
    link_destination_urls,
    link_destination_urls_for,
    parse_destination,
)

log = get_logger(__name__)

# Flat caps for all callers initially (no per-key config yet; the schema
# leaves room via reporter_ids). Anonymous is captcha-gated AND capped
# tighter — give researchers a reason to get a key.
ANON_MAX_ITEMS = 25
AUTHED_MAX_ITEMS = 100


def normalize_report_target(
    raw: str, system_domain: str
) -> tuple[str | None, str] | None:
    """Normalize a reported ``code_or_url`` to ``(domain, code)``.

    Accepts bare codes (``abc123``), schemeless short URLs
    (``spoo.me/abc123``), full URLs with query/fragment noise
    (``https://spoo.me/abc123?x=1``), and custom-domain short URLs
    (``go.customer.com/deal``).

    ``domain`` is ``None`` for the system default domain, the lowercased
    fqdn otherwise. The code is percent-decoded (emoji aliases arrive
    encoded) but its case is PRESERVED — codes are case-sensitive.
    Returns ``None`` when the input can't name a short link (bad scheme,
    no hostname, empty or multi-segment path).
    """
    value = raw.strip()
    if not value:
        return None

    if "/" not in value and "://" not in value:
        # Bare code — system default domain.
        return (None, unquote(value))

    candidate = value if "://" in value else f"https://{value}"
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https"):
        return None
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if not hostname:
        return None

    path = parsed.path.strip("/")
    if not path or "/" in path:
        # No code, or a multi-segment path — not a short link.
        return None

    code = unquote(path)
    # www.spoo.me serves the same site as spoo.me (Caddy vhost pair) —
    # reporters paste www URLs; both are the system domain.
    is_system = hostname == system_domain or hostname == f"www.{system_domain}"
    domain = None if is_system else hostname
    return (domain, code)


@dataclass
class RejectedItem:
    """One rejected entry — ``input`` echoes the raw ``code_or_url``."""

    index: int
    input: str
    code: RejectionCode


@dataclass
class SubmissionOutcome:
    """What ``submit`` hands back to the route layer."""

    submission_id: str
    accepted: int
    rejected: list[RejectedItem]


class ReportIntakeService:
    """Bulk report intake — normalization, resolution, dedupe, storage,
    operator summary notification.

    Args:
        report_repo:     Write side of the per-code ``reports`` docs.
        submission_repo: Per-POST audit trail.
        resolver:        Shared public resolver — system-domain existence
                         checks across ALL generations (v1/v2/emoji), the
                         same source of truth the redirect dispatches on.
        url_repo:        Domain-scoped v2 lookups for custom-domain codes
                         (custom domains exist only in v2).
        captcha:         Verifies anonymous submissions.
        notifier:        OpsNotifier for the summary ping (delivers to the
                         same channel as the legacy Jinja report path).
        safety_sink:     Safety-analysis enqueue for accepted targets.
                         None degrades to no analysis (reports still store
                         and notify — storage is the system of record).
    """

    def __init__(
        self,
        report_repo: ReportRepository,
        submission_repo: ReportSubmissionRepository,
        resolver: PublicLinkResolver,
        url_repo: UrlRepository,
        captcha: CaptchaProvider,
        notifier: OpsNotifier,
        *,
        system_default_domain: str,
        safety_sink: SafetySink | None = None,
    ) -> None:
        self._report_repo = report_repo
        self._submission_repo = submission_repo
        self._resolver = resolver
        self._url_repo = url_repo
        self._captcha = captcha
        self._notify = notifier
        self._system_default_domain = system_default_domain
        self._safety_sink = safety_sink

    # ── Public API ────────────────────────────────────────────────────────────

    async def submit(
        self,
        items: Sequence[ReportItemRequest],
        *,
        reporter_id: ObjectId | None,
        reporter_email: str | None,
        reporter_org: str | None,
        captcha_token: str | None,
        source: str,
        ip: str,
    ) -> SubmissionOutcome:
        """Process one report submission end to end.

        Args:
            items:          The reported links (already shape-validated).
            reporter_id:    Authenticated user's id, or ``None`` for
                            anonymous — drives the item cap and captcha.
            reporter_email: Optional follow-up contact (audit record only).
            reporter_org:   Optional organisation (audit record only).
            captcha_token:  hCaptcha token — verified for anonymous
                            submissions only.
            source:         ``"web"`` or ``"api"`` (API-key callers).
            ip:             Client IP for the audit record + embed.

        Raises:
            ValidationError: Empty items or over the caller's item cap —
                             the whole request fails (the client knows the
                             cap; partial-accept would hide it).
            ForbiddenError:  Anonymous captcha verification failed.
        """
        cap = ANON_MAX_ITEMS if reporter_id is None else AUTHED_MAX_ITEMS
        if not items:
            raise ValidationError("items must contain at least one report")
        if len(items) > cap:
            raise ValidationError(
                f"Too many items: {len(items)} exceeds the "
                f"{'anonymous' if reporter_id is None else 'authenticated'} "
                f"cap of {cap} per request"
            )

        if reporter_id is None and not await self._captcha.verify(captcha_token or ""):
            log.info("report_intake_captcha_failed")
            raise ForbiddenError("Invalid captcha, please try again")

        accepted, rejected = await self._triage(items)

        now = datetime.now(timezone.utc)
        for domain, code, item, _long_url in accepted:
            await self._report_repo.record_report(
                domain,
                code,
                reason=item.reason.value,
                vector=item.vector.value if item.vector else None,
                details=item.details,
                reporter_id=reporter_id,
                source=source,
                now=now,
            )

        submission_oid = await self._submission_repo.insert(
            {
                "created_at": now,
                "ip": ip,
                "reporter_id": reporter_id,
                "reporter_email": reporter_email,
                "reporter_org": reporter_org,
                "source": source,
                "item_count": len(items),
                "accepted": len(accepted),
                "rejected_count": len(rejected),
            }
        )
        submission_id = str(submission_oid)

        # Demoted to a notification: storage above is the system of record,
        # so a failed send is logged, never surfaced — the reports ARE filed.
        # The notifier owns formatting; the domain fact passed down is which
        # display target each accepted item resolves to.
        sent = await self._notify.report_summary(
            submission_id=submission_id,
            source=source,
            authenticated=reporter_id is not None,
            accepted=[
                (f"{domain or self._system_default_domain}/{code}", item.reason.value)
                for domain, code, item, _long_url in accepted
            ],
            rejected_count=len(rejected),
            reporter_email=reporter_email,
            reporter_org=reporter_org,
            ip=ip,
            now=now,
        )
        if not sent:
            log.error("report_summary_notify_failed", submission_id=submission_id)

        await self._enqueue_safety_analysis(accepted)

        log.info(
            "report_submission_stored",
            submission_id=submission_id,
            source=source,
            authenticated=reporter_id is not None,
            item_count=len(items),
            accepted=len(accepted),
            rejected=len(rejected),
        )
        return SubmissionOutcome(
            submission_id=submission_id,
            accepted=len(accepted),
            rejected=rejected,
        )

    # ── Private: triage ───────────────────────────────────────────────────────

    async def _triage(
        self, items: Sequence[ReportItemRequest]
    ) -> tuple[
        list[tuple[str | None, str, ReportItemRequest, str | None]], list[RejectedItem]
    ]:
        """Normalize, dedupe, and existence-check every item.

        Per item, in order: unparseable → ``invalid_input``; normalized
        (domain, code) already seen in this batch → ``duplicate_in_batch``
        (first occurrence wins, and only it is existence-checked); code
        missing from every generation → ``not_found``. Bad codes never
        sink the batch — survivors are returned for storage, each carrying
        the resolved destination long_url (captured here so the safety
        enqueue never resolves twice).
        """
        seen: set[tuple[str | None, str]] = set()
        accepted: list[tuple[str | None, str, ReportItemRequest, list[str]]] = []
        rejected: list[RejectedItem] = []

        for index, item in enumerate(items):
            target = normalize_report_target(
                item.code_or_url, self._system_default_domain
            )
            if target is None:
                rejected.append(RejectedItem(index, item.code_or_url, "invalid_input"))
                continue
            if target in seen:
                rejected.append(
                    RejectedItem(index, item.code_or_url, "duplicate_in_batch")
                )
                continue
            seen.add(target)

            domain, code = target
            destinations = await self._resolve_destinations(domain, code)
            if destinations is not None:
                accepted.append((domain, code, item, destinations))
            else:
                rejected.append(RejectedItem(index, item.code_or_url, "not_found"))

        return accepted, rejected

    async def _resolve_destinations(
        self, domain: str | None, code: str
    ) -> list[str] | None:
        """Domain-scoped existence check that also yields every destination
        the link routes to (long_url, geo overrides, A/B variants, pre-start
        and after-expiry pages).

        System-domain codes resolve via the shared PublicLinkResolver —
        the same generation dispatch (v1/v2/emoji) as the redirect, and
        status-agnostic on purpose: expired/blocked links are still
        reportable. Custom-domain codes are exact v2 lookups. Returns an
        empty list when the link exists but no destination is readable,
        or None when the code does not exist.
        """
        if domain is None:
            resolved = await self._resolver.resolve(code)
            if resolved is None:
                return None
            if resolved.v2_doc is not None:
                return link_destination_urls_for(resolved.v2_doc)
            return link_destination_urls(str((resolved.raw_v1 or {}).get("url") or ""))
        doc = await self._url_repo.find_by_alias(code, domain)
        if doc is None:
            return None
        return link_destination_urls_for(doc)

    async def _enqueue_safety_analysis(
        self, accepted: list[tuple[str | None, str, ReportItemRequest, list[str]]]
    ) -> None:
        """One analysis request per distinct destination host in the batch,
        every destination of every reported link included (many reported
        codes often point at one campaign host). Best-effort: the sink
        swallows failures and None means safety is not wired."""
        if self._safety_sink is None or not accepted:
            return
        by_host: dict[str, SafetyAnalyzeEvent] = {}
        for domain, code, item, destinations in accepted:
            reported = f"{domain or self._system_default_domain}/{code}"
            for url in destinations:
                parts = parse_destination(url)
                if parts is None:
                    continue
                host = parts["host"]
                existing = by_host.get(host)
                if existing is not None:
                    reasons = set(existing.context.get("reasons", []))
                    reasons.add(item.reason.value)
                    codes = existing.context.get("reported_codes", [])
                    known = existing.context.get("link_destinations") or [existing.url]
                    merged = [*known, *(u for u in destinations if u not in known)]
                    context = {
                        **existing.context,
                        "reasons": sorted(reasons),
                        "reported_codes": (
                            codes if reported in codes else [*codes, reported]
                        ),
                    }
                    if len(merged) > 1:
                        context["link_destinations"] = merged
                    by_host[host] = existing.model_copy(update={"context": context})
                    continue
                context: dict = {
                    "reasons": [item.reason.value],
                    "reported_codes": [reported],
                }
                if len(destinations) > 1:
                    context["link_destinations"] = destinations
                by_host[host] = SafetyAnalyzeEvent(
                    url=url,
                    host=host,
                    registrable_domain=parts["registrable_domain"],
                    trigger="report",
                    context=context,
                )
        for event in by_host.values():
            await self._safety_sink.emit(event)
