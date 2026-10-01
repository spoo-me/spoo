"""Renderer protocol — flavors are presentation, never a second schema.

A renderer turns (event type, payload) into the exact body string that
gets signed and sent. ``raw`` is the versioned contract; every other
flavor is a lossy rendering of it. Docs, catalog samples, and Zapier
speak raw only.
"""

from __future__ import annotations

from typing import Any, Protocol


class Renderer(Protocol):
    # Optional: query params the executor appends to the endpoint URL at
    # delivery time (e.g. Discord's ``with_components=true``). Absent or
    # empty means the URL is used as stored.

    flavor: str

    def render(
        self, event_id: str, event_type: str, timestamp: str, payload: dict[str, Any]
    ) -> str: ...
