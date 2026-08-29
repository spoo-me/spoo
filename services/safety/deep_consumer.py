"""Worker-side consumer for the ``events:safety:deep`` stream.

Thin wrapper mirroring SafetyAnalysisConsumer: decode
(drop-don't-poison) and delegate to the investigator. A raise leaves the
message pending for the claimer; unprocessable payloads are dropped.
"""

from __future__ import annotations

from typing import Any

from infrastructure.logging import get_logger
from services.safety.events import safety_event_from_payload
from services.safety.investigation import DeepInvestigator

log = get_logger(__name__)


class DeepAnalysisConsumer:
    def __init__(self, investigator: DeepInvestigator) -> None:
        self._investigator = investigator

    async def consume(self, payload: Any) -> None:
        event = safety_event_from_payload(payload)
        if event is None:
            return  # malformed: logged by the decoder, dropped
        await self._investigator.investigate(event)
