"""Raw flavor — the Standard Webhooks envelope, THE versioned contract."""

from __future__ import annotations

import json
from typing import Any


class RawRenderer:
    flavor = "raw"

    def render(
        self, event_id: str, event_type: str, timestamp: str, payload: dict[str, Any]
    ) -> str:
        # ``id`` is the EVENT identity (evt_…, same fact across endpoints);
        # the webhook-id header is the DELIVERY identity (msg_…, dedup key).
        # Compact separators: the rendered body is stored per delivery row
        # and capped at max_payload_bytes — no cosmetic whitespace.
        return json.dumps(
            {
                "id": event_id,
                "type": event_type,
                "timestamp": timestamp,
                "data": payload,
            },
            separators=(",", ":"),
            default=str,
        )
