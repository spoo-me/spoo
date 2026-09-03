"""Operator notifications — pings to the maintainer's Discord server.

NOT the user-facing webhooks product: this is the internal channel that
tells the operator a visitor submitted the contact form or reported a
URL. It happens to deliver over Discord webhook URLs, which is why it
used to live in ``infrastructure/webhook/`` — that name is reserved for
the real webhooks system.

Two shapes live here. The named methods (``contact_message``,
``url_report``, ``report_summary``) are semantic: callers state WHAT
happened and this module owns the whole embed. ``send_embed`` is the
generic: the caller owns title, color and field text, and this module owns
only what is Discord's business (channel routing, footer, the per-field
size cap, an image as an attachment, https-only delivery with no
redirects). Safety composes its embeds over ``send_embed`` in
``services/safety/notify.py``. Send failures return ``False`` and never
raise — callers decide whether a failed ping is fatal.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from infrastructure.http_client import HttpClient
from infrastructure.logging import get_logger
from shared.image_sniff import EXT, MIME, sniff_image

log = get_logger(__name__)

Channel = Literal["report", "contact"]


def _attachment(image: bytes) -> tuple[str, str]:
    """Discord renders the embed image only when name and type match the bytes."""
    info = sniff_image(image)
    fmt = info.format if info else "webp"
    return f"evidence.{EXT[fmt]}", MIME[fmt]


_CHANNELS = frozenset(("report", "contact"))
# Discord's per-field cap; the fences count.
_FIELD_MAX = 1024
_FOOTER = {
    "text": "spoo-me",
    "icon_url": "https://spoo.me/static/images/favicon.png",
}
_CONTACT_COLOR = 9103397
_REPORT_COLOR = 14177041

# Summary embed: list at most this many targets, then "… and N more".
_SUMMARY_MAX_LISTED = 10
_SUMMARY_LINE_MAX = 80


def _bound_field(value: str) -> str:
    """Clip a field value to Discord's limit, fences included, keeping the
    head. The full text still lives on the verdict; only the display clips."""
    if len(value) <= _FIELD_MAX:
        return value
    fenced = value.startswith("```") and value.endswith("```")
    body = value[3:-3] if fenced else value
    keep = _FIELD_MAX - (6 if fenced else 0) - 1
    clipped = body[:keep] + "…"
    return f"```{clipped}```" if fenced else clipped


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

    async def send_embed(
        self,
        *,
        channel: Channel,
        title: str,
        color: int,
        fields: list[dict[str, Any]],
        image: bytes | None = None,
        kind: str = "embed",
    ) -> bool: ...


class DiscordOpsNotifier:
    """Discord implementation — routes each notification to its channel
    (contact vs reports); builds the embeds for the named methods and
    delivers caller-built ones through ``send_embed``.

    The named methods' embed shapes are pinned by the integration tests
    (test_contact / test_reports run this class over a capturing HTTP
    fake). ``send_embed``'s field text is pinned by its callers' tests;
    only its Discord mechanics are tested here.
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

    async def send_embed(
        self,
        *,
        channel: Channel,
        title: str,
        color: int,
        fields: list[dict[str, Any]],
        image: bytes | None = None,
        kind: str = "embed",
    ) -> bool:
        """The generic operator embed. Callers own WHAT the fields say; this
        owns Discord: routing, the footer, field limits, and how an image
        rides along. Field values are bounded here because Discord rejects
        the whole message over 1024 characters and a lost notification is
        worse than a clipped one."""
        if channel not in _CHANNELS:
            # A typo must not quietly route a block embed to the contact channel.
            log.warning("ops_notify_unknown_channel", kind=kind, channel=channel)
            return False
        url = self._report_url if channel == "report" else self._contact_url
        payload = {
            "embeds": [
                {
                    "title": title,
                    "color": color,
                    "fields": [
                        {"name": f["name"], "value": _bound_field(f["value"])}
                        for f in fields
                    ],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "footer": _FOOTER,
                }
            ]
        }
        return await self._deliver(url, payload, kind=kind, image=image)

    # ── Delivery ──────────────────────────────────────────────────────────────

    async def _deliver(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        kind: str,
        image: bytes | None = None,
    ) -> bool:
        if not url:
            log.warning("ops_notify_not_configured", kind=kind)
            return False
        if not url.startswith("https://"):
            # A webhook URL is a bearer credential; never let it, or a
            # screenshot, travel in the clear or follow a redirect elsewhere.
            log.warning("ops_notify_insecure_url_refused", kind=kind)
            return False
        try:
            if image:
                # Discord webhooks take the image as a multipart file; the
                # embed refers to it by attachment name.
                name, mime = _attachment(image)
                payload["embeds"][0]["image"] = {"url": f"attachment://{name}"}
                response = await self._http.post(
                    url,
                    data={"payload_json": json.dumps(payload)},
                    files={"files[0]": (name, image, mime)},
                    follow_redirects=False,
                )
            else:
                response = await self._http.post(
                    url, json=payload, follow_redirects=False
                )
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
