"""Operator notifications — pings to the maintainer's Discord server.

NOT the user-facing webhooks product: this is the internal channel that
tells the operator a visitor submitted the contact form or reported a
URL. It happens to deliver over Discord webhook URLs, which is why it
used to live in ``infrastructure/webhook/`` — that name is reserved for
the real webhooks system.

``OpsNotifier`` is semantic: callers state WHAT happened; the
implementation owns channel routing and every Discord specific (embed
structure, colors, footer). Send failures return ``False`` and never
raise — callers decide whether a failed ping is fatal.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

from infrastructure.http_client import HttpClient
from infrastructure.logging import get_logger

log = get_logger(__name__)

_FOOTER = {
    "text": "spoo-me",
    "icon_url": "https://spoo.me/static/images/favicon.png",
}
_CONTACT_COLOR = 9103397
_REPORT_COLOR = 14177041
_SAFETY_ACTION_COLOR = 15548997  # red: automatic enforcement happened
_SAFETY_REVIEW_COLOR = 16705372  # yellow: a human decision is needed

# Summary embed: list at most this many targets, then "… and N more".
_SUMMARY_MAX_LISTED = 10
_SUMMARY_LINE_MAX = 80


class OpsNotifier(Protocol):
    async def contact_message(self, email: str, message: str) -> bool: ...

    async def url_report(
        self, short_code: str, reason: str, ip_address: str, app_url: str
    ) -> bool: ...

    async def report_summary(
        self,
        *,
        submission_id: str,
        source: str,
        authenticated: bool,
        accepted: list[tuple[str, str]],
        rejected_count: int,
        reporter_email: str | None,
        reporter_org: str | None,
        ip: str,
        now: datetime,
    ) -> bool: ...

    async def safety_action(
        self,
        *,
        host: str,
        reason: str,
        trigger: str,
        blocked_count: int,
        legacy_count: int,
        sample_url: str | None,
    ) -> bool: ...

    async def safety_review(
        self,
        *,
        host: str,
        trigger: str,
        sample_url: str | None,
        context: dict | None,
    ) -> bool: ...


class DiscordOpsNotifier:
    """Discord implementation — routes each notification to its channel
    (contact vs reports) and builds the embeds.

    Embed shapes are pinned by the integration tests (test_contact /
    test_reports run this class over a capturing HTTP fake): change a
    field here and a test breaks.
    """

    def __init__(
        self, contact_url: str, report_url: str, http_client: HttpClient
    ) -> None:
        self._contact_url = contact_url
        self._report_url = report_url
        self._http = http_client

    # ── OpsNotifier ───────────────────────────────────────────────────────────

    async def contact_message(self, email: str, message: str) -> bool:
        payload = {
            "embeds": [
                {
                    "title": "New Contact Message ✉️",
                    "color": _CONTACT_COLOR,
                    "fields": [
                        {"name": "Email", "value": f"```{email}```"},
                        {"name": "Message", "value": f"```{message}```"},
                    ],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "footer": _FOOTER,
                }
            ]
        }
        return await self._deliver(self._contact_url, payload, kind="contact_message")

    async def url_report(
        self, short_code: str, reason: str, ip_address: str, app_url: str
    ) -> bool:
        payload = {
            "embeds": [
                {
                    "title": f"URL Report for `{short_code}`",
                    "color": _REPORT_COLOR,
                    "url": f"{app_url}stats/{short_code}",
                    "fields": [
                        {"name": "Short Code", "value": f"```{short_code}```"},
                        {"name": "Reason", "value": f"```{reason}```"},
                        {"name": "IP Address", "value": f"```{ip_address}```"},
                    ],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "footer": _FOOTER,
                }
            ]
        }
        return await self._deliver(self._report_url, payload, kind="url_report")

    async def report_summary(
        self,
        *,
        submission_id: str,
        source: str,
        authenticated: bool,
        accepted: list[tuple[str, str]],
        rejected_count: int,
        reporter_email: str | None,
        reporter_org: str | None,
        ip: str,
        now: datetime,
    ) -> bool:
        """ONE embed per submission — counts, source, up to
        ``_SUMMARY_MAX_LISTED`` targets with reasons, submission id.

        ``accepted`` carries ``(display_target, reason)`` pairs; ``now``
        is the submission timestamp already stamped on the audit record,
        so the embed and the record can never disagree.
        """
        fields: list[dict[str, Any]] = [
            {"name": "Submission ID", "value": f"```{submission_id}```"},
            {
                "name": "Source",
                "value": (
                    f"```{source} · "
                    f"{'authenticated' if authenticated else 'anonymous'}```"
                ),
            },
            {
                "name": "Accepted / Rejected",
                "value": f"```{len(accepted)} / {rejected_count}```",
            },
        ]

        if accepted:
            lines = []
            for display, reason in accepted[:_SUMMARY_MAX_LISTED]:
                line = f"{display} — {reason}"
                if len(line) > _SUMMARY_LINE_MAX:
                    line = line[: _SUMMARY_LINE_MAX - 1] + "…"
                lines.append(line)
            overflow = len(accepted) - _SUMMARY_MAX_LISTED
            if overflow > 0:
                lines.append(f"… and {overflow} more")
            fields.append(
                {"name": "Reported Links", "value": "```" + "\n".join(lines) + "```"}
            )

        if reporter_email or reporter_org:
            fields.append(
                {
                    "name": "Reporter",
                    "value": f"```{reporter_email or '—'} · {reporter_org or '—'}```",
                }
            )

        fields.append({"name": "IP Address", "value": f"```{ip}```"})

        payload = {
            "embeds": [
                {
                    "title": "New URL Report Submission",
                    "color": _REPORT_COLOR,
                    "fields": fields,
                    "timestamp": now.isoformat(),
                    "footer": _FOOTER,
                }
            ]
        }
        return await self._deliver(self._report_url, payload, kind="report_summary")

    async def safety_action(
        self,
        *,
        host: str,
        reason: str,
        trigger: str,
        blocked_count: int,
        legacy_count: int,
        sample_url: str | None,
    ) -> bool:
        """Enforcement already happened — this states the ACTION TAKEN, it
        is not a request for one. ``legacy_count`` is v1/emoji links
        blocked via their safety flag (same 451 as v2, reversible)."""
        fields: list[dict[str, Any]] = [
            {"name": "Destination Host", "value": f"```{host}```"},
            {"name": "Reason", "value": f"```{reason}```"},
            {"name": "Trigger", "value": f"```{trigger}```"},
            {"name": "Links Blocked (v2)", "value": f"```{blocked_count}```"},
        ]
        if legacy_count:
            fields.append(
                {
                    "name": "Legacy v1/emoji Blocked",
                    "value": f"```{legacy_count}```",
                }
            )
        if sample_url:
            fields.append({"name": "Sample URL", "value": f"```{sample_url}```"})
        payload = {
            "embeds": [
                {
                    "title": "Safety: destination auto-blocked",
                    "color": _SAFETY_ACTION_COLOR,
                    "fields": fields,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "footer": _FOOTER,
                }
            ]
        }
        return await self._deliver(self._report_url, payload, kind="safety_action")

    async def safety_review(
        self,
        *,
        host: str,
        trigger: str,
        sample_url: str | None,
        context: dict | None,
    ) -> bool:
        """No local source could judge this destination — a human decision
        is needed. Carries the trigger context so the decision takes
        seconds, not an investigation."""
        fields: list[dict[str, Any]] = [
            {"name": "Destination Host", "value": f"```{host}```"},
            {"name": "Trigger", "value": f"```{trigger}```"},
        ]
        if sample_url:
            fields.append({"name": "Sample URL", "value": f"```{sample_url}```"})
        if context:
            lines = [f"{k}: {v}" for k, v in list(context.items())[:8]]
            fields.append(
                {"name": "Context", "value": "```" + "\n".join(lines) + "```"}
            )
        payload = {
            "embeds": [
                {
                    "title": "Safety: review needed",
                    "color": _SAFETY_REVIEW_COLOR,
                    "fields": fields,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "footer": _FOOTER,
                }
            ]
        }
        return await self._deliver(self._report_url, payload, kind="safety_review")

    # ── Delivery ──────────────────────────────────────────────────────────────

    async def _deliver(self, url: str, payload: dict[str, Any], *, kind: str) -> bool:
        if not url:
            log.warning("ops_notify_not_configured", kind=kind)
            return False
        try:
            response = await self._http.post(url, json=payload)
            if response.status_code in (200, 204):
                return True
            log.warning(
                "ops_notify_failed",
                kind=kind,
                status_code=response.status_code,
                response_text=response.text[:200],
            )
            return False
        except Exception as e:
            log.error(
                "ops_notify_request_failed",
                kind=kind,
                error=str(e),
                error_type=type(e).__name__,
            )
            return False
