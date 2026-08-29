"""Worker-side consumer for the ``events:safety`` stream.

Thin wrapper: decode (drop-don't-poison) and delegate to the analyzer.
Raising is reserved for transient infrastructure failures — a raise leaves
the message pending for the claimer; unprocessable payloads are dropped.
"""

from __future__ import annotations

from typing import Any

from infrastructure.logging import get_logger
from services.safety.analyzer import SafetyAnalyzer
from services.safety.events import safety_event_from_payload

log = get_logger(__name__)


class SafetyAnalysisConsumer:
    def __init__(self, analyzer: SafetyAnalyzer) -> None:
        self._analyzer = analyzer

    async def consume(self, payload: Any) -> None:
        event = safety_event_from_payload(payload)
        if event is None:
            return  # malformed: logged by the decoder, dropped
        await self._analyzer.analyze(event)
