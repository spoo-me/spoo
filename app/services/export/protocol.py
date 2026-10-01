"""ExportFormatter protocol — the contract every formatter must satisfy."""

from __future__ import annotations

from typing import Any

from typing_extensions import Protocol


class ExportFormatter(Protocol):
    """Minimal protocol every export formatter must satisfy."""

    mimetype: str
    filename: str

    def serialize(self, data: dict[str, Any]) -> bytes: ...
