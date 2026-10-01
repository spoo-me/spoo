"""Shared copy for the lossy flavors — one place decides wording and which
fields earn space, so Discord and Slack never drift apart.

Every event renders as label/value pairs with guaranteed substance: a
click always carries Device and From (a referrer-less click is a
"direct" visit, not a blank), and lifecycle cards state "never" and
"unlimited" as the real answers they are. Event types this module does
not know —
including every future addition to the registry — fall back to the payload
as a JSON code block, so adding an event can never terminal-fail a
flavored endpoint.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

EVENT_LABELS = {
    "link.created": "Link created",
    "link.updated": "Link updated",
    "link.deleted": "Link deleted",
    "link.expired": "Link expired",
    "link.clicked": "Click",
    "webhook.test": "Test delivery",
}

_EXPIRY_REASONS = {
    "max_clicks_reached": "Max clicks reached",
    "time_expired": "Expiry time passed",
}

# Analytics sentinels are for querying, not for humans — a dimension that
# resolved to one renders as absent, same as None.
_SENTINELS = {"", "unknown", "(none)"}

_VALUE_MAX = 512  # per rendered value; receivers cap harder, this is copy-level
_CODE_MAX = 1_500  # fallback JSON block


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _present(value: Any) -> bool:
    if value is None:
        return False
    return not (isinstance(value, str) and value.strip().lower() in _SENTINELS)


def _fmt(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (dict, list)):
        return clip(json.dumps(value, separators=(",", ":"), default=str), _VALUE_MAX)
    return clip(str(value), _VALUE_MAX)


@dataclass(frozen=True)
class Copy:
    """Flavor-neutral content. Exactly one of ``lines`` / ``pairs`` /
    ``changes`` / ``code`` is populated (compact, field grid, old/new
    change set, or fallback code block).

    ``changes`` stays structured — (field, old, new) — because flavors
    disagree on the best rendering: Discord has colored diff blocks,
    Slack only has plain pairs."""

    title: str
    title_url: str | None
    # ``context`` is the full footer line (brand + time + notice) for flavors
    # without a native timestamp slot; ``notice`` alone is for flavors that
    # render time themselves.
    context: str
    notice: str | None = None
    # Title split for flavors that compose their own heading markup, and
    # the destination as a one-line subtitle for lifecycle events.
    label: str = ""
    display: str | None = None
    subtitle: str | None = None
    lines: list[str] = field(default_factory=list)
    pairs: list[tuple[str, str]] = field(default_factory=list)
    changes: list[tuple[str, str, str]] = field(default_factory=list)
    code: str | None = None


def _link_of(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    if event_type == "link.clicked":
        return payload
    link = payload.get("link")
    return link if isinstance(link, dict) else {}


def _title(event_type: str, link: dict[str, Any]) -> str:
    label = EVENT_LABELS.get(event_type, event_type)
    alias, domain = link.get("alias"), link.get("domain")
    if _present(alias) and _present(domain):
        return f"{label}: {domain}/{alias}"
    return label


def _notice(payload: dict[str, Any]) -> str | None:
    dropped = payload.get("dropped_since_last")
    if isinstance(dropped, int) and dropped > 0:
        plural = "delivery" if dropped == 1 else "deliveries"
        return f"{dropped} earlier {plural} dropped"
    return None


def _referrer_host(referrer: Any) -> str:
    text = str(referrer)
    host = urlparse(text).netloc
    return host or text


def _date_part(value: Any) -> str:
    return str(value)[:10] if _present(value) else "never"


def _age_days(created_at: Any, occurred_at: str) -> int | None:
    try:
        created = datetime.fromisoformat(str(created_at))
        occurred = datetime.fromisoformat(occurred_at)
        return max(0, (occurred - created).days)
    except (TypeError, ValueError):
        return None


def _country_display(code: Any) -> str:
    """ISO alpha-2 → flag emoji + code; anything else passes through."""
    text = str(code)
    if len(text) == 2 and text.isascii() and text.isalpha():
        upper = text.upper()
        flag = "".join(chr(0x1F1E6 + ord(c) - 65) for c in upper)
        return f"{flag} {upper}"
    return text


def _clicked_pairs(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """One field per dimension — the airy grid IS the point. Absent
    analytics dimensions are skipped, but a referrer-less click IS a
    direct visit and the running count is always stated, so a card is
    never empty."""
    pairs: list[tuple[str, str]] = []

    if _present(payload.get("country")):
        pairs.append(("Country", _country_display(payload["country"])))
    if _present(payload.get("city")):
        pairs.append(("City", str(payload["city"])))
    if _present(payload.get("browser")):
        pairs.append(("Browser", str(payload["browser"])))
    if _present(payload.get("os")):
        pairs.append(("OS", str(payload["os"])))
    if _present(payload.get("device")):
        pairs.append(("Device", str(payload["device"])))

    referrer = payload.get("referrer")
    pairs.append(("From", _referrer_host(referrer) if _present(referrer) else "direct"))

    utm = payload.get("utm")
    if isinstance(utm, dict):
        campaign = " / ".join(str(v) for v in utm.values() if _present(v))
        if campaign:
            pairs.append(("UTM", campaign))

    if payload.get("is_bot"):
        bot = payload.get("bot_name")
        pairs.append(("Bot", str(bot) if _present(bot) else "detected"))

    total = payload.get("total_clicks")
    if isinstance(total, int):
        # Count at click time — the payload snapshot precedes the increment.
        pairs.append(("Clicks so far", f"{total:,}"))

    return [(name, clip(value, _VALUE_MAX)) for name, value in pairs]


def _updated_changes(payload: dict[str, Any]) -> list[tuple[str, str, str]]:
    changes = payload.get("changes")
    if not isinstance(changes, dict):
        return []
    out: list[tuple[str, str, str]] = []
    for name, change in changes.items():
        old = _fmt(change.get("old")) if isinstance(change, dict) else _fmt(None)
        new = _fmt(change.get("new")) if isinstance(change, dict) else _fmt(change)
        out.append((str(name), old, new))
    return out


def _lifecycle_pairs(
    event_type: str, payload: dict[str, Any], occurred_at: str
) -> list[tuple[str, str]]:
    """Lifecycle cards lead with facts that always exist — an expiry of
    "never" and a cap of "unlimited" are real answers, not blanks."""
    link = _link_of(event_type, payload)
    pairs: list[tuple[str, str]] = []
    total = link.get("total_clicks")

    if event_type == "link.expired":
        reason = payload.get("reason")
        pairs.append(("Reason", _EXPIRY_REASONS.get(reason, _fmt(reason))))

    if _present(link.get("long_url")):
        pairs.append(("Destination", _fmt(link["long_url"])))

    if event_type == "link.created":
        pairs.append(("Expires", _date_part(link.get("expires_at"))))
        max_clicks = link.get("max_clicks")
        pairs.append(
            (
                "Max clicks",
                f"{max_clicks:,}" if isinstance(max_clicks, int) else "unlimited",
            )
        )
        pairs.append(
            ("Password", "protected" if link.get("password_protected") else "none")
        )
        pairs.append(("Bots", "blocked" if link.get("block_bots") else "allowed"))
        pairs.append(("Meta tags", "custom" if link.get("meta_tags") else "default"))
        tags = link.get("tag_ids")
        pairs.append(
            (
                "Tags",
                f"{len(tags)} {'tag' if len(tags) == 1 else 'tags'}"
                if isinstance(tags, list) and tags
                else "none",
            )
        )
        geo = link.get("geo_rules")
        pairs.append(
            (
                "Geo targeting",
                f"{len(geo)} {'rule' if len(geo) == 1 else 'rules'}"
                if isinstance(geo, dict) and geo
                else "none",
            )
        )

    if event_type in ("link.deleted", "link.expired") and isinstance(total, int):
        pairs.append(("Lifetime clicks", f"{total:,}"))
    if event_type == "link.deleted":
        age = _age_days(link.get("created_at"), occurred_at)
        if age is not None:
            pairs.append(("Age", f"{age:,} days" if age != 1 else "1 day"))

    return pairs


def clicked_summary(
    payload: dict[str, Any], escape: Callable[[str], str] | None = None
) -> tuple[list[str], str | None]:
    """Grouped one-per-topic lines for vertical layouts (Components V2),
    plus the running count for the footer.

    ``escape`` runs on every payload-derived value BEFORE this function
    adds its own markup. These values are visitor-controlled (the
    referrer above all): a flavor whose text renders markup must
    neutralize them, or a crafted Referer header plants live markdown in
    the subscriber's channel."""
    esc = escape or (lambda s: s)
    lines: list[str] = []
    if _present(payload.get("country")):
        line = _country_display_bold(payload["country"], esc)
        if _present(payload.get("city")):
            line += f"  {esc(str(payload['city']))}"
        lines.append(line)
    elif _present(payload.get("city")):
        lines.append(esc(str(payload["city"])))

    device_bits = [
        esc(str(payload[k])) for k in ("browser", "os") if _present(payload.get(k))
    ]
    device = " on ".join(device_bits)
    if _present(payload.get("device")):
        part = esc(str(payload["device"]))
        device = f"{device}, {part}" if device else part
    if device:
        lines.append(device)

    referrer = payload.get("referrer")
    source = (
        f"from **{esc(_referrer_host(referrer))}**"
        if _present(referrer)
        else "direct visit"
    )
    utm = payload.get("utm")
    if isinstance(utm, dict):
        campaign = " / ".join(esc(str(v)) for v in utm.values() if _present(v))
        if campaign:
            source += f"  ({campaign})"
    lines.append(source)

    if payload.get("is_bot"):
        bot = payload.get("bot_name")
        lines.append(f"bot  **{esc(str(bot))}**" if _present(bot) else "bot traffic")

    total = payload.get("total_clicks")
    count = f"{total:,}" if isinstance(total, int) else None
    return [clip(line, _VALUE_MAX) for line in lines], count


def _country_display_bold(code: Any, esc: Callable[[str], str]) -> str:
    text = str(code)
    if len(text) == 2 and text.isascii() and text.isalpha():
        upper = text.upper()
        flag = "".join(chr(0x1F1E6 + ord(c) - 65) for c in upper)
        return f"{flag} **{upper}**"
    return esc(str(code))


def build_copy(event_type: str, timestamp: str, payload: dict[str, Any]) -> Copy:
    link = _link_of(event_type, payload)
    title = _title(event_type, link)
    url = link.get("short_url") if _present(link.get("short_url")) else None
    notice = _notice(payload)
    context = f"spoo.me · {timestamp}" + (f" · {notice}" if notice else "")
    label = EVENT_LABELS.get(event_type, event_type)
    display = (
        f"{link.get('domain')}/{link.get('alias')}"
        if _present(link.get("alias")) and _present(link.get("domain"))
        else None
    )
    subtitle = (
        str(link.get("long_url"))
        if event_type in ("link.created", "link.deleted", "link.expired")
        and _present(link.get("long_url"))
        else None
    )

    common = dict(label=label, display=display, subtitle=subtitle)
    if event_type == "link.clicked":
        return Copy(
            title, url, context, notice, pairs=_clicked_pairs(payload), **common
        )
    if event_type == "webhook.test":
        message = payload.get("message")
        lines = [clip(str(message), _VALUE_MAX)] if _present(message) else []
        return Copy(title, url, context, notice, lines=lines, **common)
    if event_type == "link.updated":
        return Copy(
            title, url, context, notice, changes=_updated_changes(payload), **common
        )
    if event_type in ("link.created", "link.deleted", "link.expired"):
        return Copy(
            title,
            url,
            context,
            notice,
            pairs=_lifecycle_pairs(event_type, payload, timestamp),
            **common,
        )

    shown = {k: v for k, v in payload.items() if k != "dropped_since_last"}
    code = clip(json.dumps(shown, separators=(",", ":"), default=str), _CODE_MAX)
    return Copy(title, url, context, notice, code=code, **common)
