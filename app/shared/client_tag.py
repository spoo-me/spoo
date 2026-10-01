"""
X-Spoo-Client header parsing — shared by the logging middleware and routes.

First-party clients identify themselves with ``X-Spoo-Client: <slug>`` or
``<slug>/<version>`` (e.g. ``snap/2.1.0``). Values that do not match the
shape are treated as absent, never rejected — the header is advisory
telemetry, not an auth surface.
"""

from __future__ import annotations

import re

CLIENT_TAG_HEADER = "X-Spoo-Client"

_CLIENT_TAG_RE = re.compile(r"^([a-z0-9_-]{1,32})(?:/([A-Za-z0-9._-]{1,16}))?$")

# Slugs sent by first-party clients. Logging accepts any shape-valid slug
# (a new client shows up in queries without a deploy); durable persistence
# (created_via) accepts only members, so arbitrary header values can never
# become permanent, unfilterable document history.
FIRST_PARTY_CLIENTS = frozenset(
    {
        "dashboard",
        "landing",
        "snap",
        "raycast",
        "cli",
        "bot",
        "sdk-ts",
        "sdk-py",
        "sdk-go",
        "sdk-rust",
        "sdk-kotlin",
        "app-android",
    }
)


def parse_client_tag(raw: str | None) -> tuple[str | None, str | None]:
    """Parse an X-Spoo-Client value into ``(client, client_version)``."""
    match = _CLIENT_TAG_RE.match((raw or "").strip())
    if match is None:
        return None, None
    return match.group(1), match.group(2)


def first_party_client(raw: str | None) -> str | None:
    """The parsed client slug if it names a first-party client, else None.

    Use this for values that get persisted; use ``parse_client_tag`` where
    unknown-but-well-formed slugs should still be visible (logging).
    """
    client, _ = parse_client_tag(raw)
    return client if client in FIRST_PARTY_CLIENTS else None
