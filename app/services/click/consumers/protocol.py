"""ClickConsumer protocol — the contract every consumer group class satisfies.

Lives beside its implementers (stats, hotness) the same way ClickHandler
lives beside the schema handlers; the worker is just a host that binds
these to FastStream subscribers.
"""

from __future__ import annotations

from typing import Any

from typing_extensions import Protocol


class ClickConsumer(Protocol):
    """Processes one decoded stream payload for its consumer group.

    Raise to leave the message pending (retry via the claim path); treat
    permanently-unprocessable payloads as handled (log + return) so they
    never poison the group.
    """

    async def consume(self, payload: Any) -> None: ...
