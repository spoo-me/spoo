"""Discord flavor — a Components V2 message, ready for a channel webhook.

The rendered body is a components-only message (flag 32768): one
accent-colored container holding a heading, a divider, the event's
substance, and a small-text footer with a timestamp Discord localizes
per viewer (``<t:...>``). Delivery must carry ``with_components=true``
on the webhook URL — declared here via ``url_query`` and appended by
the executor, since the signature covers the body alone.

Mentions are hard-disabled: text displays CAN ping (embeds never
could), and payload values are user-controlled.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, ClassVar

from app.services.webhooks.renderers.copy import build_copy, clicked_summary, clip

_COLORS = {
    "link.created": 0x43B581,
    "link.updated": 0x5865F2,
    "link.deleted": 0xF04747,
    "link.expired": 0xE67E22,
    "link.clicked": 0x99AAB5,
    "webhook.test": 0x99AAB5,
}
_DEFAULT_COLOR = 0x99AAB5

AVATAR_URL = "https://spoo.me/static/images/favicon.png"

# Footer verb per event; ``f`` renders an absolute local time, ``R`` a
# live relative one — moments for high-frequency events, dates for rare.
_FOOTERS = {
    "link.clicked": ("click", "R"),
    "link.created": ("created", "f"),
    "link.updated": ("updated", "R"),
    "link.deleted": ("deleted", "f"),
    "link.expired": ("expired", "R"),
    "webhook.test": ("sent", "R"),
}

_TEXT_MAX = 3_500  # defensive budget across all text displays
_SUBTITLE_MAX = 200


def _td(content: str) -> dict[str, Any]:
    return {"type": 10, "content": content}


def _sep(spacing: int = 1, divider: bool = False) -> dict[str, Any]:
    return {"type": 14, "spacing": spacing, "divider": divider}


_MARKDOWN_CHARS = set("\\`*_~|>#[]()")


def _md(text: str) -> str:
    """Backslash-escape Discord markdown in user-derived text. Payload
    values reach live text displays, and text displays render markup —
    without this a crafted Referer header plants a clickable masked link
    in the subscriber's channel."""
    return "".join("\\" + ch if ch in _MARKDOWN_CHARS else ch for ch in text)


def _link_target(url: str) -> str:
    # Parentheses inside a masked-link target close the link early.
    return url.replace("(", "%28").replace(")", "%29")


def _fence(text: str) -> str:
    # A value containing ``` would break out of the block.
    return text.replace("```", "'''")


class DiscordRenderer:
    flavor = "discord"
    # Appended to the endpoint URL at delivery time; components-v2 bodies
    # are rejected without it.
    url_query: ClassVar[dict[str, str]] = {"with_components": "true"}

    def render(
        self, event_id: str, event_type: str, timestamp: str, payload: dict[str, Any]
    ) -> str:
        copy = build_copy(event_type, timestamp, payload)
        try:
            epoch = int(datetime.fromisoformat(timestamp).timestamp())
        except ValueError:
            epoch = None

        label = copy.label or copy.title
        components: list[dict[str, Any]] = []

        # Heading: linked short address, except a deleted link has no
        # living address to point at.
        if copy.display and copy.title_url and event_type != "link.deleted":
            components.append(
                _td(
                    f"### {_md(label)}  "
                    f"[{_md(copy.display)}]({_link_target(copy.title_url)})"
                )
            )
        elif copy.display:
            components.append(_td(f"### {_md(label)}  {_md(copy.display)}"))
        else:
            components.append(_td(f"### {_md(label)}"))
        if copy.subtitle:
            components.append(_td(f"-# {_md(clip(copy.subtitle, _SUBTITLE_MAX))}"))

        lifecycle = event_type in ("link.created", "link.deleted", "link.expired")
        components.append(_sep(2 if lifecycle else 1, divider=True))

        count: str | None = None
        if event_type == "link.clicked":
            lines, count = clicked_summary(payload, escape=_md)
            components.append(_td("\n".join(lines)))
        elif copy.changes:
            diff_lines: list[str] = []
            for name, old, new in copy.changes:
                diff_lines.append(f"- {name}: {_fence(old)}")
                diff_lines.append(f"+ {name}: {_fence(new)}")
            components.append(_td("```diff\n" + "\n".join(diff_lines) + "\n```"))
        elif copy.pairs:
            components.append(
                _td(
                    "\n".join(
                        f"**{name}**  {_md(value)}"
                        for name, value in copy.pairs
                        if name != "Destination"  # the subtitle already says it
                    )
                )
            )
        elif copy.code:
            components.append(_td("```json\n" + _fence(copy.code) + "\n```"))
        elif copy.lines:
            components.append(_td("\n".join(_md(line) for line in copy.lines)))

        components.append(_sep(2))
        verb, style = _FOOTERS.get(event_type, ("received", "f"))
        footer = f"-# {verb}"
        if event_type == "link.clicked" and count is not None:
            footer += f" {count}"
        if epoch is not None:
            footer += f"   <t:{epoch}:{style}>"
        if copy.notice:
            footer += f"\n-# {copy.notice}"
        components.append(_td(footer))

        # Defensive budget: receivers reject oversize messages whole, so a
        # pathological body gets its largest block clipped instead.
        total = sum(len(c.get("content", "")) for c in components)
        if total > _TEXT_MAX:
            biggest = max(components, key=lambda c: len(c.get("content", "")))
            overflow = total - _TEXT_MAX
            biggest["content"] = clip(
                biggest["content"], max(1, len(biggest["content"]) - overflow)
            )

        return json.dumps(
            {
                "username": "spoo.me",
                "avatar_url": AVATAR_URL,
                "flags": 32768,
                "allowed_mentions": {"parse": []},
                "components": [
                    {
                        "type": 17,
                        "accent_color": _COLORS.get(event_type, _DEFAULT_COLOR),
                        "components": components,
                    }
                ],
            },
            separators=(",", ":"),
            default=str,
        )
