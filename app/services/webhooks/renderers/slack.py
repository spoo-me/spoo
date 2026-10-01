"""Slack flavor — Block Kit body for a Slack incoming webhook URL.

Top-level ``text`` is the notification fallback Slack shows in toasts and
unexpanded previews. Receiver limits enforced locally (section text 3000,
10 fields per section); Slack code blocks do not colorize, so link.updated
renders old → new pairs instead of Discord's diff block.
"""

from __future__ import annotations

import json
from typing import Any

from app.services.webhooks.renderers.copy import build_copy, clip

_TEXT_MAX = 3000
_FIELD_MAX = 2000
_FIELDS_PER_SECTION = 10


def _escape(text: str) -> str:
    # Slack mrkdwn control characters.
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _mrkdwn(text: str) -> dict[str, str]:
    return {"type": "mrkdwn", "text": clip(text, _TEXT_MAX)}


def _title_text(title: str, url: str | None) -> str:
    escaped = _escape(title)
    return f"*<{url}|{escaped}>*" if url else f"*{escaped}*"


def _field_sections(pairs: list[tuple[str, str]]) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    for start in range(0, len(pairs), _FIELDS_PER_SECTION):
        chunk = pairs[start : start + _FIELDS_PER_SECTION]
        sections.append(
            {
                "type": "section",
                "fields": [
                    _mrkdwn(clip(f"*{_escape(name)}*\n{_escape(value)}", _FIELD_MAX))
                    for name, value in chunk
                ],
            }
        )
    return sections


class SlackRenderer:
    flavor = "slack"

    def render(
        self, event_id: str, event_type: str, timestamp: str, payload: dict[str, Any]
    ) -> str:
        copy = build_copy(event_type, timestamp, payload)

        blocks: list[dict[str, Any]] = []
        if copy.lines:
            body = "\n".join(_escape(line) for line in copy.lines)
            blocks.append(
                {
                    "type": "section",
                    "text": _mrkdwn(
                        f"{_title_text(copy.title, copy.title_url)}\n{body}"
                    ),
                }
            )
        else:
            blocks.append(
                {
                    "type": "section",
                    "text": _mrkdwn(_title_text(copy.title, copy.title_url)),
                }
            )
            if copy.changes:
                blocks.extend(
                    _field_sections(
                        [(name, f"{old} → {new}") for name, old, new in copy.changes]
                    )
                )
            elif copy.pairs:
                blocks.extend(_field_sections(copy.pairs))
            elif copy.code:
                blocks.append(
                    {"type": "section", "text": _mrkdwn(f"```{_escape(copy.code)}```")}
                )

        blocks.append({"type": "context", "elements": [_mrkdwn(_escape(copy.context))]})

        return json.dumps(
            {"text": clip(copy.title, _TEXT_MAX), "blocks": blocks},
            separators=(",", ":"),
            default=str,
        )
